"""OncoKB-first point mutation classification, fail-closed batches, retries, and caching tests."""

from __future__ import annotations

import io
import json
import sqlite3
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError

import numpy as np
import pytest
from alteration_fixtures import maf
from test_alterations import (
    CATALOG,
    CNA_DRIVER,
    DRIVER,
    IDS,
    ROW,
    build_fixture,
    category,
    records,
    rich_inputs,
    sample,
    synthetic,
)

from gene_expression_relevance import (
    ALTERATION_PARAMS,
    POINT_MUTATION,
    alterations,
    drivers,
    oncokb,
)
from gene_expression_relevance import build as builder

_REAL_LOAD_INPUTS = alterations.load_inputs


def _fake_oncokb_requester(
    cancer_genes,
    annotations_by_key,
    *,
    info_version="v7.7",
    info_date="10/29/2026",
    calls=None,
    on_batch=None,
):
    """Build an in-memory HTTP stub mimicking OncoKB /info, /cancerGeneList, /byProteinChange."""

    def requester(url, *, payload=None, token=None, **kwargs):
        if calls is not None:
            calls.append((url, payload, token))
        if url.endswith("/info"):
            return {"dataVersion": {"version": info_version, "date": info_date}}
        if url.endswith("/utils/cancerGeneList"):
            return cancer_genes
        if url.endswith("/annotate/mutations/byProteinChange"):
            assert payload is not None
            if on_batch is not None:
                custom = on_batch(payload)
                if custom is not None:
                    return custom
            out = []
            for item in payload:
                sym = item["gene"]["hugoSymbol"]
                alt = item["alteration"]
                conseq = item.get("consequence", "")
                genome = item.get("referenceGenome", "GRCh37")
                verdict = annotations_by_key.get(
                    (sym, alt, conseq, genome),
                    annotations_by_key.get((sym, alt), "Unknown"),
                )
                out.append(
                    {
                        "query": {"id": item["id"], "hugoSymbol": sym, "alteration": alt},
                        "oncogenic": verdict,
                        "mutationEffect": {"knownEffect": verdict},
                        "variantExist": verdict != "Unknown",
                        "geneExist": sym in {g["hugoSymbol"] for g in cancer_genes},
                    }
                )
            return out
        raise AssertionError(f"Unexpected URL: {url}")

    return requester


CGL = [
    {"hugoSymbol": "KRAS", "entrezGeneId": 3845, "geneType": "ONCOGENE", "geneAliases": []},
    {"hugoSymbol": "EGFR", "entrezGeneId": 1956, "geneType": "ONCOGENE", "geneAliases": ["ERBB1"]},
    {"hugoSymbol": "TP53", "entrezGeneId": 7157, "geneType": "TSG", "geneAliases": ["P53"]},
]


def test_classification_rules_exact_classes_and_predicted_oncogenic_excluded(tmp_path):
    """Verify OncoKB classes: Resistance counts; Predicted Oncogenic/Inconclusive/Neutral do not."""
    calls = []
    req = _fake_oncokb_requester(
        CGL,
        {
            ("KRAS", "Q61H"): "Oncogenic",
            ("TP53", "R196*"): "Likely Oncogenic",
            ("EGFR", "T790M"): "Resistance",
            # Predicted Oncogenic: distinct 'predicted_oncogenic' status, NO count & NO fallback.
            ("EGFR", "L858R"): "Predicted Oncogenic",
            # Likely Neutral / Neutral / Inconclusive: excluded even at >=10 hotspot or TSG.
            ("KRAS", "G12A"): "Likely Neutral",
            ("KRAS", "G12S"): "Neutral",
            ("TP53", "Y220C"): "Inconclusive",
            ("TP53", "Q100*"): "Predicted Oncogenic",
            # Unknown falls back when recurrent hotspot or TSG truncating.
            ("KRAS", "G12V"): "Unknown",
        },
        calls=calls,
    )
    assert oncokb.classify_oncogenic("Predicted Oncogenic") == "predicted_oncogenic"
    assert oncokb.classify_oncogenic("Resistance") == "oncokb"
    assert oncokb.classify_oncogenic("Inconclusive") == "inconclusive"
    assert oncokb.classify_oncogenic("Likely Neutral") == "neutral"
    assert oncokb.classify_oncogenic("Unknown") == "unknown"

    client = oncokb.OncoKBClient(tmp_path, token="tok-secret", requester=req)
    try:
        rows = [
            # Oncogenic, Likely Oncogenic, Resistance -> count under 'oncokb'.
            ("KRAS", sample("AA", 0), "Missense_Mutation", "T1", 61, "p.Q61H"),
            ("TP53", sample("AA", 0), "Nonsense_Mutation", "T3", 196, "p.R196*"),
            ("EGFR", sample("AA", 0), "Missense_Mutation", "T2", 790, "p.T790M"),
            # Patient AA-0001 has BOTH Oncogenic + Unknown in KRAS -> single 'oncokb' event.
            ("KRAS", sample("AA", 1), "Missense_Mutation", "T1", 61, "p.Q61H"),
            ("KRAS", sample("AA", 1), "Missense_Mutation", "T1", 12, "p.G12V"),
            # KRAS pos 12: 8 G12V (+AA-0001 = 9) + 1 G12A (Likely Neutral) + 1 G12S (Neutral).
            # Recurrence counts ALL 11 retained tumors at pos 12 regardless of OncoKB verdict;
            # Unknown G12V falls back to hotspot while Likely Neutral / Neutral still never count.
            *[
                ("KRAS", sample("AA", i), "Missense_Mutation", "T1", 12, "p.G12V")
                for i in range(2, 10)
            ],
            ("KRAS", sample("AA", 10), "Missense_Mutation", "T1", 12, "p.G12A"),
            ("KRAS", sample("AA", 11), "Missense_Mutation", "T1", 12, "p.G12S"),
            # Predicted Oncogenic (10 at EGFR 858, TP53 Q100* truncating) -> never falls back.
            *[
                ("EGFR", sample("AA", i), "Missense_Mutation", "T2", 858, "p.L858R")
                for i in range(2, 12)
            ],
            ("TP53", sample("AA", 12), "Nonsense_Mutation", "T3", 100, "p.Q100*"),
            # Inconclusive (10 tumors at TP53 220) -> NEVER counts or falls back.
            *[
                ("TP53", sample("AA", i), "Missense_Mutation", "T3", 220, "p.Y220C")
                for i in range(2, 12)
            ],
            # Empty-HGVSp Splice_Site in TSG TP53 -> hotspot fallback.
            ("TP53", sample("AA", 1), "Splice_Site", "T3", -1, ""),
            # Non-CGL gene ROS1 >=10 missense -> hotspot fallback; truncating in non-TSG -> ignored.
            *[
                ("ROS1", sample("AA", i), "Missense_Mutation", "T4", 2032, "p.G2032R")
                for i in range(10)
            ],
            ("ROS1", sample("AA", 15), "Nonsense_Mutation", "T4", 10, "p.Q10*"),
        ]
        retained = {r[1] for r in rows}
        excluded_counts = {}
        events, sources = alterations.classify_point_mutations(
            records(rows),
            retained,
            {ROW["TP53"]},
            ALTERATION_PARAMS,
            oncokb=client,
            row_symbols=[CATALOG[e] for e in IDS],
            excluded_counts=excluded_counts,
        )
        assert sources[(ROW["KRAS"], sample("AA", 0))] == "oncokb"
        assert sources[(ROW["KRAS"], sample("AA", 1))] == "oncokb"
        assert sources[(ROW["TP53"], sample("AA", 0))] == "oncokb"
        assert sources[(ROW["EGFR"], sample("AA", 0))] == "oncokb"
        assert sources[(ROW["TP53"], sample("AA", 1))] == "hotspot"
        for i in range(2, 12):
            assert (ROW["EGFR"], sample("AA", i)) not in events
            assert (ROW["TP53"], sample("AA", i)) not in events
        assert (ROW["TP53"], sample("AA", 12)) not in events
        for i in range(2, 10):
            assert sources[(ROW["KRAS"], sample("AA", i))] == "hotspot"
        for i in (10, 11):
            assert (ROW["KRAS"], sample("AA", i)) not in events
        for i in range(10):
            assert sources[(ROW["ROS1"], sample("AA", i))] == "hotspot"
        assert (ROW["ROS1"], sample("AA", 15)) not in events
        assert excluded_counts == {
            "predicted_oncogenic": 11,
            "inconclusive": 10,
            "neutral": 2,
        }
        queried_genes = {
            item["gene"]["hugoSymbol"]
            for url, payload, _ in calls
            if url.endswith("byProteinChange") and payload
            for item in payload
        }
        assert "ROS1" not in queried_genes
    finally:
        client.close()


def test_inframe_and_missense_position_hotspot_grouping_and_tsg_fallback(tmp_path):
    """Both missense and in-frame indels require >=10 tumors at the same protein position."""
    maf_path = tmp_path / "positions.txt"
    rows = [
        # EGFR exon 19: 5 E746_A750del + 3 E746_S752delinsV + 2 missense E746K = 10 at pos 746.
        *[
            ("EGFR", 1956, "In_Frame_Del", sample("AA", i), "T1", "p.E746_A750del")
            for i in range(5)
        ],
        *[
            ("EGFR", 1956, "In_Frame_Del", sample("AA", i), "T1", "p.E746_S752delinsV")
            for i in range(5, 8)
        ],
        *[
            ("EGFR", 1956, "Missense_Mutation", sample("AA", i), "T1", "p.E746K")
            for i in range(8, 10)
        ],
        # Exon 20 ins: only 9 identical tumors -> <10, must NOT count.
        *[
            ("EGFR", 1956, "In_Frame_Ins", sample("BB", i), "T1", "p.D770_N771insG")
            for i in range(9)
        ],
    ]
    maf_path.write_text(maf(rows))
    parsed = alterations.parse_mutations(maf_path)
    retained = {r[3] for r in rows}
    mapped = {
        "row": [ROW["EGFR"]] * len(rows),
        "sample": parsed["sample_table"][parsed["sample"]],
        "cls": parsed["cls"],
        "transcript": parsed["transcript_table"][parsed["transcript"]],
        "position": parsed["position"],
        "hgvsp": parsed["hgvsp_table"][parsed["hgvsp"]],
    }
    events, _ = alterations.classify_point_mutations(mapped, retained, set(), ALTERATION_PARAMS)
    assert events == {(ROW["EGFR"], sample("AA", i)) for i in range(10)}


def test_batch_validation_reordered_ok_and_malformed_failures_cache_nothing(tmp_path):
    """Matched by query.id (never position); malformed/error responses fail and cache nothing."""
    keys = [
        ("KRAS", "G12D", "missense_variant", "GRCh37"),
        ("EGFR", "L858R", "missense_variant", "GRCh37"),
    ]

    # Reordered valid responses succeed and assign exact per-id oncogenic verdicts.
    def reversed_batch(payload):
        if payload[0]["id"] == "preflight":
            return [{"query": {"id": "preflight"}, "oncogenic": "Oncogenic"}]
        return [
            {
                "query": {"id": payload[1]["id"]},
                "oncogenic": "Likely Neutral",
            },
            {
                "query": {"id": payload[0]["id"]},
                "oncogenic": "Unknown",
            },
        ]

    ok_client = oncokb.OncoKBClient(
        tmp_path / "ok",
        token="secret",
        requester=_fake_oncokb_requester(CGL, {}, on_batch=reversed_batch),
    )
    try:
        # Sorted: EGFR L858R -> payload[0] -> Unknown; KRAS G12D -> payload[1] -> Likely Neutral.
        out = ok_client.annotate(keys)
        assert out[("EGFR", "L858R", "missense_variant", "GRCh37")] == "unknown"
        assert out[("KRAS", "G12D", "missense_variant", "GRCh37")] == "neutral"
    finally:
        ok_client.close()

    bad_cases = [
        "missing_id",
        "duplicate_id",
        "unknown_id",
        "missing_oncogenic",
        "invalid_oncogenic",
        "error_entry",
    ]
    for idx, kind in enumerate(bad_cases):
        def bad_batch(payload, _kind=kind):
            if payload[0]["id"] == "preflight":
                return [{"query": {"id": "preflight"}, "oncogenic": "Oncogenic"}]
            if _kind == "missing_id":
                return [
                    {"oncogenic": "Oncogenic"},
                    {"query": {"id": payload[1]["id"]}, "oncogenic": "Oncogenic"},
                ]
            if _kind == "duplicate_id":
                return [
                    {"query": {"id": payload[0]["id"]}, "oncogenic": "Oncogenic"},
                    {"query": {"id": payload[0]["id"]}, "oncogenic": "Oncogenic"},
                ]
            if _kind == "unknown_id":
                return [
                    {"query": {"id": payload[0]["id"]}, "oncogenic": "Oncogenic"},
                    {"query": {"id": "wrong-id"}, "oncogenic": "Oncogenic"},
                ]
            if _kind == "missing_oncogenic":
                return [
                    {"query": {"id": payload[0]["id"]}, "oncogenic": "Oncogenic"},
                    {"query": {"id": payload[1]["id"]}},
                ]
            if _kind == "invalid_oncogenic":
                return [
                    {"query": {"id": payload[0]["id"]}, "oncogenic": "Oncogenic"},
                    {"query": {"id": payload[1]["id"]}, "oncogenic": None},
                ]
            return [
                {"query": {"id": payload[0]["id"]}, "oncogenic": "Oncogenic"},
                {
                    "query": {"id": payload[1]["id"]},
                    "oncogenic": "Unknown",
                    "errors": ["invalid alteration"],
                },
            ]

        client = oncokb.OncoKBClient(
            tmp_path / f"bad-{idx}",
            token="secret",
            requester=_fake_oncokb_requester(CGL, {}, on_batch=bad_batch),
        )
        try:
            with pytest.raises(oncokb.OncoKBError):
                client.annotate(keys)
            conn = sqlite3.connect(client.cache_db_path)
            count = conn.execute("SELECT COUNT(*) FROM annotations").fetchone()[0]
            conn.close()
            assert count == 0, f"{kind} must not cache partial batch"
        finally:
            client.close()


def test_retries_backoff_http_date_uncapped_seconds_and_fail_fast_4xx(monkeypatch):
    """5xx/429/network retry with uncapped Retry-After (sec + HTTP-date); 4xx non-429 fail fast."""
    secret = "secret-oncokb-uuid-value"

    # 1. Fail-fast on 400, 401, 403 without retries.
    for code, exc_cls in (
        (400, oncokb.OncoKBError),
        (401, oncokb.OncoKBAuthError),
        (403, oncokb.OncoKBAuthError),
    ):
        attempts = []

        def fail_once(req, timeout=60.0, _code=code, _attempts=attempts):
            _attempts.append(req.full_url)
            raise HTTPError(req.full_url, _code, "err", {}, io.BytesIO(b""))

        monkeypatch.setattr(oncokb, "urlopen", fail_once)
        with pytest.raises(exc_cls):
            oncokb.request_json("https://www.oncokb.org/api/v1/info", token=secret, attempts=4)
        assert len(attempts) == 1

    # 2. Retry 502 + URLError + 429 with numeric Retry-After > 60s (120s, uncapped) and HTTP-date.
    now = datetime(2026, 11, 18, 12, 0, 0, tzinfo=UTC).timestamp()
    sleeps = []
    attempts = []

    class FakeResp:
        def __enter__(self):
            return io.BytesIO(b'{"dataVersion": {"version": "v7.7"}}')

        def __exit__(self, *args):
            return False

    def flaky(req, timeout=60.0):
        attempts.append(len(attempts))
        n = len(attempts)
        if n == 1:
            raise HTTPError(req.full_url, 502, "Bad Gateway", {}, io.BytesIO(b""))
        if n == 2:
            raise URLError(f"reset {secret}")
        if n == 3:
            raise HTTPError(
                req.full_url,
                429,
                "Too Many Requests",
                {"Retry-After": "120"},
                io.BytesIO(b""),
            )
        if n == 4:
            # 95s later via HTTP-date (also > 60s, uncapped).
            return_dt = "Wed, 18 Nov 2026 12:01:35 GMT"
            raise HTTPError(
                req.full_url,
                429,
                "Too Many Requests",
                {"Retry-After": return_dt},
                io.BytesIO(b""),
            )
        return FakeResp()

    monkeypatch.setattr(oncokb, "urlopen", flaky)
    res = oncokb.request_json(
        "https://www.oncokb.org/api/v1/info",
        token=secret,
        attempts=5,
        sleep=sleeps.append,
        wall_clock=lambda: now,
        jitter=lambda: 0.25,
    )
    assert res == {"dataVersion": {"version": "v7.7"}}
    assert len(attempts) == 5
    assert sleeps == [1.25, 2.25, 120.25, 95.25]  # exponential+jitter, uncapped >60s & HTTP-date


def test_deadline_mid_annotation_marks_incomplete_keeps_cache_and_resumes(
    tmp_path, monkeypatch, capsys
):
    """Deadline mid-run saves batches, writes oncokb-report, refuses publish, and resumes."""
    cache = build_fixture(tmp_path, monkeypatch, rich_inputs)
    # Add a second TP53 variant so batch_size=1 requires 2 batches.
    original_load = alterations.load_inputs

    def two_variant_inputs(commit, c, studies, **kwargs):
        inputs, prov, sha = original_load(commit, c, studies, **kwargs)
        samples = inputs["LIHC"]["mutation"]["data"]["sample_table"]
        s1 = samples[0]
        path = tmp_path / "extra_lihc.txt"
        path.write_text(
            maf(
                [
                    ("TP53", 7157, "Nonsense_Mutation", s1, "T", "p.R196*"),
                    ("TP53", 7157, "Missense_Mutation", s1, "T", "p.R175H"),
                ]
            )
        )
        inputs["LIHC"]["mutation"]["data"] = alterations.parse_mutations(path)
        return inputs, prov, sha

    monkeypatch.setattr(alterations, "load_inputs", two_variant_inputs)

    calls_run1 = []
    tick = [0.0]

    def fake_clock():
        return tick[0]

    def requester_run1(url, *, payload=None, token=None, **kwargs):
        res = _fake_oncokb_requester(
            CGL,
            {("TP53", "R196*"): "Likely Oncogenic", ("TP53", "R175H"): "Oncogenic"},
            calls=calls_run1,
        )(url, payload=payload, token=token, **kwargs)
        if url.endswith("byProteinChange"):
            # Advance clock past budget right after the first batch succeeds.
            tick[0] += 100.0
        return res

    monkeypatch.setattr(
        oncokb,
        "load_client",
        lambda cache_dir: oncokb.OncoKBClient(
            cache_dir,
            token="super-secret-token",
            requester=requester_run1,
            batch_size=1,
            time_budget_seconds=50.0,
            clock=fake_clock,
        ),
    )

    out = tmp_path / "site"
    with pytest.raises(oncokb.OncoKBBudgetExceeded):
        builder.build("a" * 40, out, cache, skip_precheck=True)

    captured = capsys.readouterr().out
    assert "OncoKB annotated 1/2 variants (1 remaining, batch 1/2" in captured
    assert "super-secret-token" not in captured

    inc = tmp_path / "site-incomplete"
    inc_manifest = json.loads((inc / "manifest.json").read_text())
    inc_report = json.loads((inc / "oncokb-report.json").read_text())
    assert inc_manifest["source"]["oncokb_incomplete"] is True
    assert inc_manifest["source"]["oncokb_complete"] is False
    assert inc_report["status"] == "incomplete"
    assert inc_report["progress"]["annotated"] == 1 and inc_report["progress"]["remaining"] == 1
    assert not (out / "v1/manifest.json").exists()

    # Publishing an incomplete manifest must be refused.
    import importlib.util
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path("scripts").resolve()))
    spec = importlib.util.spec_from_file_location("pub_mod", "scripts/publish.py")
    publisher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(publisher)
    bundle_dir = tmp_path / "inc-bundle"
    bundle_dir.mkdir()
    (bundle_dir / "manifest.json").write_text(json.dumps(inc_manifest))
    with pytest.raises(ValueError, match="PubMed|OncoKB"):
        publisher.publish(bundle_dir)

    # Next run resumes with cached batch 1 and only queries the remaining 1 variant!
    calls_run2 = []
    monkeypatch.setattr(
        oncokb,
        "load_client",
        lambda cache_dir: oncokb.OncoKBClient(
            cache_dir,
            token="super-secret-token",
            requester=_fake_oncokb_requester(
                CGL,
                {("TP53", "R196*"): "Likely Oncogenic", ("TP53", "R175H"): "Oncogenic"},
                calls=calls_run2,
            ),
            batch_size=1,
        ),
    )
    manifest2 = builder.build("a" * 40, out, cache, skip_precheck=True)
    assert manifest2["source"]["oncokb_complete"] is True
    assert manifest2["source"]["oncokb_incomplete"] is False
    post_queries_2 = [
        item["alteration"]
        for url, payload, _ in calls_run2
        if url.endswith("byProteinChange") and payload
        for item in payload
    ]
    assert post_queries_2 == ["R196*"]  # R175H came from incremental SQLite cache!


def test_cache_namespace_hashes_exact_version_without_sanitization_collision(tmp_path):
    """Two dataVersion strings that sanitize identically get distinct cache directories."""
    keys = [("KRAS", "G12D", "missense_variant", "GRCh37")]
    c1 = oncokb.OncoKBClient(
        tmp_path,
        token="tok",
        requester=_fake_oncokb_requester(CGL, {("KRAS", "G12D"): "Oncogenic"}, info_version="v7/7"),
    )
    try:
        res1 = c1.annotate(keys)
        p1 = c1.cache_db_path
    finally:
        c1.close()

    c2 = oncokb.OncoKBClient(
        tmp_path,
        token="tok",
        requester=_fake_oncokb_requester(
            CGL, {("KRAS", "G12D"): "Likely Neutral"}, info_version="v7_7"
        ),
    )
    try:
        res2 = c2.annotate(keys)
        p2 = c2.cache_db_path
    finally:
        c2.close()

    assert p1 != p2
    assert res1[keys[0]] == "oncokb"
    assert res2[keys[0]] == "neutral"


def test_missing_token_fails_closed_unless_hotspot_only(tmp_path, monkeypatch):
    """Missing ONCOKB_API_TOKEN fails non-hotspot build; --hotspot-only runs and marks build."""
    monkeypatch.delenv("ONCOKB_API_TOKEN", raising=False)
    monkeypatch.setattr(oncokb, "load_client", oncokb.OncoKBClient)
    with pytest.raises(oncokb.OncoKBAuthError, match="ONCOKB_API_TOKEN is required"):
        oncokb.load_client(tmp_path)

    syn_dir = tmp_path / "syn"
    syn_dir.mkdir()
    inputs, mapping, selection = synthetic(syn_dir)
    result = alterations.compute(
        inputs, mapping, selection, IDS, CATALOG, {}, DRIVER, cna_driver=CNA_DRIVER, oncokb=None
    )
    assert result.summary["point_mutation_mode"] == "hotspot_only"
    assert result.record(ROW["KRAS"], 0)["categories"][POINT_MUTATION]["sources"] == {
        "oncokb": 0,
        "hotspot": 11,
    }


def test_tsg_fallback_respects_oncokb_role_over_bailey_possible_tsg(tmp_path):
    """OncoKB ONCOGENE classification suppresses Bailey 'possible tsg' truncating fallback."""
    cgl = [
        {"hugoSymbol": "EGFR", "entrezGeneId": 1956, "geneType": "ONCOGENE", "geneAliases": []},
        {"hugoSymbol": "TP53", "entrezGeneId": 7157, "geneType": "TSG", "geneAliases": []},
    ]
    driver = drivers.DriverSource(
        name="bailey-odd",
        sha256="0" * 64,
        genes=frozenset({"EGFR", "TP53", "ROS1"}),
        tumor_suppressors=frozenset({"EGFR", "TP53", "ROS1"}),
    )
    syn_dir = tmp_path / "syn"
    syn_dir.mkdir()
    inputs, mapping, selection = synthetic(syn_dir)
    s = selection["AA"][1]
    path = tmp_path / "splice.txt"
    path.write_text(
        maf(
            [
                ("EGFR", 1956, "Splice_Site", s, "T1", ""),
                ("TP53", 7157, "Splice_Site", s, "T2", ""),
                ("ROS1", 6098, "Splice_Site", s, "T3", ""),
            ]
        )
    )
    inputs["AA"]["mutation"]["data"] = alterations.parse_mutations(path)
    client = oncokb.OncoKBClient(tmp_path, token="t", requester=_fake_oncokb_requester(cgl, {}))
    try:
        res = alterations.compute(
            inputs,
            mapping,
            selection,
            IDS,
            CATALOG,
            {},
            driver,
            cna_driver=CNA_DRIVER,
            oncokb=client,
        )
        assert category(res, "EGFR", "AA", POINT_MUTATION)["n_events"] == 0
        assert category(res, "TP53", "AA", POINT_MUTATION)["n_events"] == 1
        assert category(res, "ROS1", "AA", POINT_MUTATION)["n_events"] == 1
    finally:
        client.close()


def test_release_assets_never_contain_per_variant_oncokb_data_and_version_changes_release_id(
    tmp_path, monkeypatch
):
    """Manifest/gene artifacts omit per-variant OncoKB data and track OncoKB version changes."""
    cache = build_fixture(tmp_path, monkeypatch, rich_inputs)
    releases = {}
    for ver in ("v7.7", "v7.8"):
        req = _fake_oncokb_requester(
            CGL,
            {("TP53", "R196*"): "Likely Oncogenic"},
            info_version=ver,
        )
        monkeypatch.setattr(
            oncokb,
            "load_client",
            lambda cache_dir, _r=req: oncokb.OncoKBClient(
                cache_dir, token="secret-tok", requester=_r
            ),
        )
        out = tmp_path / f"out-{ver}"
        builder.build("a" * 40, out, cache, skip_precheck=True)
        manifest = json.loads((out / "v1/manifest.json").read_text())
        tp53 = json.loads((out / "v1/genes/TP53.json").read_text())
        raw_text = (out / "v1/manifest.json").read_text() + (out / "v1/genes/TP53.json").read_text()
        assert "R196*" not in raw_text
        assert "secret-tok" not in raw_text
        assert manifest["source"]["oncokb_data_version"] == ver
        assert manifest["source"]["oncokb_complete"] is True
        assert manifest["build"]["hotspot_only"] is False
        lihc_pm = tp53["cancer_types"][0]["alterations"]["categories"][POINT_MUTATION]
        assert lihc_pm["sources"] == {"oncokb": 15, "hotspot": 0}
        releases[ver] = manifest["release_id"]
        assert not list(out.rglob("*.sqlite*"))
    assert releases["v7.7"] != releases["v7.8"]


def test_check_build_invalidates_on_oncokb_version_and_hotspot_only(tmp_path, monkeypatch):
    """check_build detects OncoKB data version bumps and keeps hotspot_only non-deployable."""
    import runpy
    from datetime import UTC, datetime

    checker = runpy.run_path("scripts/check_build.py")
    from gene_expression_relevance import (
        ALTERATION_METHOD_VERSION,
        ALTERATION_PARAMS,
        METHOD_VERSION,
        PARAMS,
        provenance,
    )

    manifest = dict(
        schema_version="1",
        method_version=METHOD_VERSION,
        params=PARAMS,
        alteration_method_version=ALTERATION_METHOD_VERSION,
        alteration_params=ALTERATION_PARAMS,
        source=dict(
            datahub_commit="a" * 40,
            code_sha256=provenance.code_fingerprint(),
            hgnc_sha256=provenance.hgnc_pin()["sha256"],
            generated_at=datetime.now(UTC).isoformat(),
            cohort_mapping_sha256="map",
            oncokb_data_version="v7.7",
            oncokb_classification_version=oncokb.CLASSIFICATION_VERSION,
            hotspot_only=False,
        ),
        build=dict(genes=None, skip_precheck=False, skip_alterations=False, hotspot_only=False),
    )
    monkeypatch.setitem(checker["main"].__globals__, "published_manifest", lambda: manifest)
    monkeypatch.setitem(checker["main"].__globals__, "current_mapping_sha", lambda c: "map")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("INPUT_COMMIT", "a" * 40)
    monkeypatch.setenv("INPUT_GENES", "")
    monkeypatch.setenv("INPUT_SKIP", "false")

    monkeypatch.setenv("INPUT_HOTSPOT_ONLY", "false")
    monkeypatch.setitem(checker["main"].__globals__, "current_oncokb_version", lambda: "v7.8")
    out1 = tmp_path / "out1"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out1))
    checker["main"]()
    vals1 = dict(line.split("=", 1) for line in out1.read_text().splitlines())
    assert vals1["changed"] == "true" and vals1["deployable"] == "true"

    monkeypatch.setenv("INPUT_HOTSPOT_ONLY", "true")
    out2 = tmp_path / "out2"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out2))
    checker["main"]()
    vals2 = dict(line.split("=", 1) for line in out2.read_text().splitlines())
    assert vals2["changed"] == "true" and vals2["deployable"] == "false"


def test_hotspot_recurrence_dominant_transcript_verdict_agnostic_and_minus_one(tmp_path):
    """Recurrence counts all retained tumors on dominant transcript regardless of verdict."""
    cgl = [
        {"hugoSymbol": "KRAS", "entrezGeneId": 3845, "geneType": "ONCOGENE", "geneAliases": []},
        {"hugoSymbol": "EGFR", "entrezGeneId": 1956, "geneType": "ONCOGENE", "geneAliases": []},
        {"hugoSymbol": "TP53", "entrezGeneId": 7157, "geneType": "TSG", "geneAliases": []},
    ]
    req = _fake_oncokb_requester(
        cgl,
        {
            ("KRAS", "G12A"): "Likely Neutral",
            ("KRAS", "G12V"): "Unknown",
            ("EGFR", "E746_A750del"): "Unknown",
            ("EGFR", "E746K"): "Unknown",
            ("TP53", "Y220C"): "Unknown",
        },
    )
    client = oncokb.OncoKBClient(tmp_path, token="tok", requester=req)
    try:
        rows = [
            # 1) 6 Likely Neutral + 4 Unknown on dominant T1 at KRAS pos 12 -> 4 Unknown count.
            *[
                ("KRAS", sample("AA", i), "Missense_Mutation", "T1", 12, "p.G12A")
                for i in range(6)
            ],
            *[
                ("KRAS", sample("AA", i), "Missense_Mutation", "T1", 12, "p.G12V")
                for i in range(6, 10)
            ],
            # 2) EGFR pos 746: 6 In_Frame_Del + 4 Missense on dominant T1 -> 10 hotspot;
            #    plus 3 rows on non-dominant T2 (at pos 746 and pos 858) -> excluded.
            *[
                ("EGFR", sample("BB", i), "In_Frame_Del", "T1", 746, "p.E746_A750del")
                for i in range(6)
            ],
            *[
                ("EGFR", sample("BB", i), "Missense_Mutation", "T1", 746, "p.E746K")
                for i in range(6, 10)
            ],
            *[
                ("EGFR", sample("BB", i), "Missense_Mutation", "T2", 746, "p.E746K")
                for i in range(10, 12)
            ],
            ("EGFR", sample("BB", 12), "Missense_Mutation", "T2", 858, "p.L858R"),
            # 3) TP53: 6 on dominant T1 and 4 on non-dominant T2 at pos 220 -> <10 on T1.
            *[
                ("TP53", sample("CC", i), "Missense_Mutation", "T1", 220, "p.Y220C")
                for i in range(6)
            ],
            *[
                ("TP53", sample("CC", i), "Missense_Mutation", "T2", 220, "p.Y220C")
                for i in range(6, 10)
            ],
            # 4) 12 tumors at position -1 on dominant ROS1 T4 -> position -1 NEVER groups!
            *[
                ("ROS1", sample("DD", i), "Missense_Mutation", "T4", -1, "")
                for i in range(12)
            ],
        ]
        retained = {r[1] for r in rows}
        excluded_counts = {}
        non_dom_counts = {}
        events, sources = alterations.classify_point_mutations(
            records(rows),
            retained,
            set(),
            ALTERATION_PARAMS,
            oncokb=client,
            row_symbols=[CATALOG[e] for e in IDS],
            excluded_counts=excluded_counts,
            non_dominant_counts=non_dom_counts,
        )
        # KRAS: only the 4 Unknown tumors at pos 12 become hotspot; Likely Neutral excluded.
        assert {s for r, s in events if r == ROW["KRAS"]} == {sample("AA", i) for i in range(6, 10)}
        for i in range(6, 10):
            assert sources[(ROW["KRAS"], sample("AA", i))] == "hotspot"
        assert excluded_counts["neutral"] == 6

        # EGFR: 10 T1 tumors at pos 746 count; T2 rows at pos 746 and 858 are excluded.
        assert {s for r, s in events if r == ROW["EGFR"]} == {sample("BB", i) for i in range(10)}
        # TP53: T1 only has 6 tumors (T2's 4 rows excluded) -> not recurrent.
        assert {s for r, s in events if r == ROW["TP53"]} == set()
        # ROS1: position -1 never groups.
        assert {s for r, s in events if r == ROW["ROS1"]} == set()
        assert non_dom_counts == {"EGFR": 3, "TP53": 4}
    finally:
        client.close()


def test_build_manifest_reports_non_dominant_transcript_exclusions(tmp_path, monkeypatch):
    """Build manifest.alterations reports non-dominant transcript exclusion counts by gene."""
    cache = build_fixture(tmp_path, monkeypatch, rich_inputs)
    orig_load = alterations.load_inputs

    def multi_tx_inputs(commit, c, studies, **kwargs):
        inputs, prov, sha = orig_load(commit, c, studies, **kwargs)
        samples = inputs["LIHC"]["mutation"]["data"]["sample_table"][:5]
        path = tmp_path / "multi_tx.txt"
        path.write_text(
            maf(
                [
                    *[
                        ("TP53", 7157, "Missense_Mutation", s, "T_DOM", "p.R175H")
                        for s in samples[:3]
                    ],
                    *[
                        ("TP53", 7157, "Missense_Mutation", s, "T_ALT", "p.R175H")
                        for s in samples[:2]
                    ],
                ]
            )
        )
        inputs["LIHC"]["mutation"]["data"] = alterations.parse_mutations(path)
        return inputs, prov, sha

    monkeypatch.setattr(alterations, "load_inputs", multi_tx_inputs)
    out = tmp_path / "out"
    manifest = builder.build("a" * 40, out, cache, skip_precheck=True, hotspot_only=True)
    alt = manifest["alterations"]
    assert alt["point_mutation_non_dominant_transcript_rows"] == 2
    assert alt["point_mutation_non_dominant_transcript_genes"] == 1
    assert alt["point_mutation_non_dominant_transcript_counts_by_gene"] == {"TP53": 2}


def test_startup_deadline_covers_info_cgl_and_cold_inputs_with_clean_exit(tmp_path, monkeypatch):
    """Slow /info, /utils/cancerGeneList, and cold input loading fail cleanly to incomplete."""
    import sys

    from gene_expression_relevance import __main__ as cli

    real_load_inputs = _REAL_LOAD_INPUTS
    (tmp_path / "a").mkdir()
    # Subcase A: Slow /info exceeds build_deadline -> TimeoutError -> incomplete artifacts + exit.
    cache_a = build_fixture(tmp_path / "a", monkeypatch, rich_inputs)
    settings_a = json.loads(oncokb.RUNTIME_SETTINGS_PATH.read_text())
    settings_a["build_time_budget_seconds"] = 5.0
    settings_path_a = tmp_path / "settings_a.json"
    settings_path_a.write_text(json.dumps(settings_a))
    monkeypatch.setattr(oncokb, "RUNTIME_SETTINGS_PATH", settings_path_a)
    tick_a = [0.0]
    info_timeouts = []

    def fake_urlopen_info(req, timeout=60.0):
        info_timeouts.append(timeout)
        tick_a[0] += 6.0
        raise TimeoutError("slow /info")

    monkeypatch.setattr(oncokb, "urlopen", fake_urlopen_info)
    monkeypatch.setattr(
        oncokb,
        "load_client",
        lambda cache_dir, build_deadline=None: oncokb.OncoKBClient(
            cache_dir,
            token="tok",
            clock=lambda: tick_a[0],
            sleep=lambda s: None,
            build_deadline=build_deadline,
        ),
    )
    out_a = tmp_path / "site-a"
    with pytest.raises(oncokb.OncoKBBudgetExceeded):
        builder.build("a" * 40, out_a, cache_a, skip_precheck=True, clock=lambda: tick_a[0])
    assert info_timeouts and info_timeouts[0] <= 5.0
    rep_a = json.loads((tmp_path / "site-a-incomplete/oncokb-report.json").read_text())
    assert rep_a["status"] == "incomplete"
    inc_m_a = json.loads((tmp_path / "site-a-incomplete/manifest.json").read_text())
    assert inc_m_a["source"]["oncokb_incomplete"] is True
    assert inc_m_a["source"]["oncokb_complete"] is False
    assert not (out_a / "v1/manifest.json").exists()

    # Verify CLI __main__.main catches start-up OncoKBBudgetExceeded cleanly via SystemExit(2).
    tick_a[0] = 0.0
    monkeypatch.setattr(cli, "_PROCESS_STARTED", 0.0)
    monkeypatch.setattr(builder.time, "monotonic", lambda: tick_a[0])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prog",
            "build",
            "--datahub-commit",
            "a" * 40,
            "--out",
            str(tmp_path / "cli-out"),
            "--cache",
            str(cache_a),
            "--skip-precheck",
        ],
    )
    with pytest.raises(SystemExit) as sys_exc:
        cli.main()
    assert sys_exc.value.code == 2
    assert (tmp_path / "cli-out-incomplete/oncokb-report.json").exists()

    # Subcase B: Fast /info, slow /utils/cancerGeneList -> incomplete exit cleanly.
    tick_b = [0.0]

    def cgl_slow_requester(url, *, payload=None, token=None, remaining=None, **kwargs):
        rem = remaining()
        assert rem <= 5.0
        if url.endswith("/info"):
            return {"dataVersion": {"version": "v7.7", "date": "10/29/2026"}}
        assert url.endswith("/utils/cancerGeneList")
        tick_b[0] += 10.0
        remaining()
        return CGL

    monkeypatch.setattr(
        oncokb,
        "load_client",
        lambda cache_dir, build_deadline=None: oncokb.OncoKBClient(
            cache_dir,
            token="tok",
            requester=cgl_slow_requester,
            clock=lambda: tick_b[0],
            sleep=lambda s: None,
            build_deadline=build_deadline,
        ),
    )
    out_b = tmp_path / "site-b"
    with pytest.raises(oncokb.OncoKBBudgetExceeded):
        builder.build("a" * 40, out_b, cache_a, skip_precheck=True, clock=lambda: tick_b[0])
    inc_m_b = json.loads((tmp_path / "site-b-incomplete/manifest.json").read_text())
    assert inc_m_b["source"]["oncokb_incomplete"] is True

    # Subcase C: Fast client, slow cold input loading -> deadline checked between input files.
    tick_c = [0.0]
    studies_loaded = []

    def slow_summary(commit, c, study, kind, **kwargs):
        studies_loaded.append((study, kind))
        tick_c[0] += 3.0
        return "oid", {
            "sample_table": np.array(["S1"], dtype=str),
            "symbol_table": np.array(["TP53"], dtype=str),
            "hgvsp_table": np.array([""], dtype=str),
            "transcript_table": np.array(["T1"], dtype=str),
            "consequence_table": np.array([""], dtype=str),
            "build_table": np.array(["GRCh37"], dtype=str),
            "sample": np.array([0]),
            "symbol": np.array([0]),
            "entrez": np.array([7157]),
            "cls": np.array([0]),
            "transcript": np.array([0]),
            "position": np.array([-1]),
            "hgvsp": np.array([0]),
            "consequence": np.array([0]),
            "build": np.array([0]),
            "n_tumors": 1,
        }

    monkeypatch.setattr(alterations, "summary", slow_summary)
    monkeypatch.setattr(alterations, "case_list", lambda *a, **kw: ("sha", {"S1"}))
    out_c = tmp_path / "site-c"
    tick_c[0] = 0.0
    monkeypatch.setattr(
        oncokb,
        "load_client",
        lambda cache_dir, build_deadline=None: oncokb.OncoKBClient(
            cache_dir,
            token="tok",
            requester=_fake_oncokb_requester(CGL, {}),
            clock=lambda: tick_c[0],
            sleep=lambda s: None,
            build_deadline=build_deadline,
        ),
    )
    monkeypatch.setattr(
        alterations,
        "load_inputs",
        lambda commit, c, studies, **kw: real_load_inputs(commit, c, studies, workers=1, **kw),
    )
    with pytest.raises(oncokb.OncoKBBudgetExceeded):
        builder.build("a" * 40, out_c, cache_a, skip_precheck=True, clock=lambda: tick_c[0])
    # Stopped between files (2 * 3s > 5s budget) rather than running all 96.
    assert len(studies_loaded) == 2
    rep_c = json.loads((tmp_path / "site-c-incomplete/oncokb-report.json").read_text())
    assert rep_c["status"] == "incomplete"


def test_server_response_values_are_redacted_and_truncated_in_errors_and_logs(tmp_path, caplog):
    """Response error, id, and oncogenic fields are redacted and truncated in errors and logs."""
    secret = "tok-very-secret-oncokb-value-987654"
    long_tail = "X" * 200

    def make_fn(kind):
        def fn(p):
            if p[0]["id"] == "preflight":
                return [{"query": {"id": "preflight"}, "oncogenic": "Oncogenic"}]
            if kind == "err":
                return [
                    {
                        "query": {"id": p[0]["id"]},
                        "oncogenic": "Unknown",
                        "error": f"bad {secret} {long_tail}",
                    }
                ]
            if kind == "qid":
                return [{"query": {"id": f"bad-{secret}-{long_tail}"}, "oncogenic": "Unknown"}]
            return [{"query": {"id": p[0]["id"]}, "oncogenic": f"Weird-{secret}-{long_tail}"}]

        return fn

    key = ("KRAS", "G12D", "missense_variant", "GRCh37")
    with caplog.at_level("DEBUG"):
        for idx, kind in enumerate(("err", "qid", "onc")):
            client = oncokb.OncoKBClient(
                tmp_path / f"c-{idx}",
                token=secret,
                requester=_fake_oncokb_requester(CGL, {}, on_batch=make_fn(kind)),
            )
            try:
                with pytest.raises(oncokb.OncoKBError) as exc_info:
                    client.annotate([key])
                msg = str(exc_info.value)
                assert secret not in msg, f"token leaked in {kind}"
                assert "[REDACTED]" in msg
                assert len(msg) < 200
            finally:
                client.close()
    assert secret not in caplog.text
