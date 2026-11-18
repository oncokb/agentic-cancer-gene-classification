"""OncoKB-first point mutation classification, caching, provenance, and fail-closed tests."""

from __future__ import annotations

import io
import json
from urllib.error import HTTPError

import pytest
from alteration_fixtures import maf
from test_alterations import (
    CATALOG,
    CNA_DRIVER,
    DRIVER,
    IDS,
    ROW,
    category,
    records,
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


def _fake_oncokb_requester(
    cancer_genes,
    annotations_by_key,
    *,
    info_version="v7.7",
    info_date="10/29/2026",
    calls=None,
    status_sequence=None,
):
    """Build an in-memory HTTP stub mimicking OncoKB /info, /cancerGeneList, /byProteinChange."""
    seq = list(status_sequence or [])

    def requester(url, *, payload=None, token=None, **kwargs):
        if calls is not None:
            calls.append((url, payload, token))
        if url.endswith("/info"):
            return {"dataVersion": {"version": info_version, "date": info_date}}
        if url.endswith("/utils/cancerGeneList"):
            return cancer_genes
        if url.endswith("/annotate/mutations/byProteinChange"):
            if seq:
                next_item = seq.pop(0)
                if isinstance(next_item, Exception):
                    raise next_item
                if next_item in (401, 403):
                    raise oncokb.OncoKBAuthError(f"HTTP {next_item}")
            assert payload is not None
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
                        "query": {"id": item.get("id"), "hugoSymbol": sym, "alteration": alt},
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


def test_classification_rules_oncokb_first_neutral_excluded_and_fallback(tmp_path):
    """Verify positive/neutral/unknown/non-CGL classification and per-patient priority."""
    calls = []
    req = _fake_oncokb_requester(
        CGL,
        {
            # Oncogenic in non-recurrent position -> counts as 'oncokb'.
            ("KRAS", "Q61H"): "Oncogenic",
            # Predicted Oncogenic / Likely Oncogenic -> counts as 'oncokb'.
            ("EGFR", "L858R"): "Predicted Oncogenic",
            ("TP53", "R196*"): "Likely Oncogenic",
            # Likely Neutral even at a 10-tumor recurrent hotspot -> excluded, NO fallback!
            ("KRAS", "G12A"): "Likely Neutral",
            # Unknown at recurrent hotspot -> falls back to 'hotspot'.
            ("KRAS", "G12V"): "Unknown",
            # Inconclusive -> falls back when recurrent hotspot or TSG truncating.
            ("TP53", "Y220C"): "Inconclusive",
        },
        calls=calls,
    )
    client = oncokb.OncoKBClient(tmp_path, token="tok-secret", requester=req)
    try:
        rows = [
            # Non-recurrent Oncogenic -> oncokb
            ("KRAS", sample("AA", 0), "Missense_Mutation", "T1", 61, "p.Q61H"),
            # Patient AA-0001 has BOTH Oncogenic + Unknown in KRAS -> single 'oncokb' event.
            ("KRAS", sample("AA", 1), "Missense_Mutation", "T1", 61, "p.Q61H"),
            ("KRAS", sample("AA", 1), "Missense_Mutation", "T1", 12, "p.G12V"),
            # 10 tumors at KRAS pos 12: 9 G12V (1 + 8, Unknown) + 1 G12A (Likely Neutral).
            # Neutral excluded -> only 9 non-neutral tumors at pos 12 -> NOT recurrent (<10)!
            *[
                ("KRAS", sample("AA", i), "Missense_Mutation", "T1", 12, "p.G12V")
                for i in range(2, 10)
            ],
            ("KRAS", sample("AA", 10), "Missense_Mutation", "T1", 12, "p.G12A"),
            # EGFR rare Unknown missense (not recurrent) -> excluded; L858R -> oncokb.
            ("EGFR", sample("AA", 0), "Missense_Mutation", "T2", 858, "p.L858R"),
            ("EGFR", sample("AA", 1), "Missense_Mutation", "T2", 100, "p.A100T"),
            # TP53: R196* -> oncokb; empty Splice_Site in TSG -> hotspot fallback;
            # Y220C Inconclusive at >=10 pos across cohorts -> hotspot fallback.
            ("TP53", sample("AA", 0), "Nonsense_Mutation", "T3", 196, "p.R196*"),
            ("TP53", sample("AA", 1), "Splice_Site", "T3", -1, ""),
            *[
                ("TP53", sample("AA", i), "Missense_Mutation", "T3", 220, "p.Y220C")
                for i in range(2, 12)
            ],
            # ROS1 is NOT in CGL: Unknown -> hotspot fallback if >=10 missense!
            *[
                ("ROS1", sample("AA", i), "Missense_Mutation", "T4", 2032, "p.G2032R")
                for i in range(10)
            ],
            # ROS1 truncating in non-TSG -> NOT counted.
            ("ROS1", sample("AA", 15), "Nonsense_Mutation", "T4", 10, "p.Q10*"),
        ]
        retained = {r[1] for r in rows}
        row_symbols = [CATALOG[e] for e in IDS]
        events, sources = alterations.classify_point_mutations(
            records(rows),
            retained,
            {ROW["TP53"]},
            ALTERATION_PARAMS,
            oncokb=client,
            row_symbols=row_symbols,
        )
        assert sources[(ROW["KRAS"], sample("AA", 0))] == "oncokb"
        assert sources[(ROW["KRAS"], sample("AA", 1))] == "oncokb"
        # Likely Neutral KRAS G12A is never counted, and 9 non-neutral G12V < 10.
        assert (ROW["KRAS"], sample("AA", 2)) not in events
        assert (ROW["KRAS"], sample("AA", 10)) not in events
        assert sources[(ROW["EGFR"], sample("AA", 0))] == "oncokb"
        assert (ROW["EGFR"], sample("AA", 1)) not in events
        assert sources[(ROW["TP53"], sample("AA", 0))] == "oncokb"
        assert sources[(ROW["TP53"], sample("AA", 1))] == "hotspot"
        assert sources[(ROW["TP53"], sample("AA", 2))] == "hotspot"
        for i in range(10):
            assert sources[(ROW["ROS1"], sample("AA", i))] == "hotspot"
        assert (ROW["ROS1"], sample("AA", 15)) not in events
        # Non-CGL gene ROS1 must never generate byProteinChange queries.
        queried_genes = {
            item["gene"]["hugoSymbol"]
            for url, payload, _ in calls
            if url.endswith("byProteinChange") and payload
            for item in payload
        }
        assert "ROS1" not in queried_genes
    finally:
        client.close()


def test_oncokb_client_batches_dedups_caches_and_rebuilds_on_version_change(tmp_path):
    """Deduplication, chunked batching, SQLite cache hits, and version-keyed isolation."""
    calls = []
    req1 = _fake_oncokb_requester(
        CGL,
        {("KRAS", "G12D"): "Oncogenic", ("EGFR", "L858R"): "Oncogenic"},
        info_version="v7.7",
        calls=calls,
    )
    c1 = oncokb.OncoKBClient(tmp_path, token="my-super-secret", requester=req1, batch_size=1)
    try:
        keys = [
            ("KRAS", "G12D", "missense_variant", "GRCh37"),
            ("KRAS", "G12D", "missense_variant", "GRCh37"),
            ("EGFR", "L858R", "missense_variant", "GRCh37"),
        ]
        res1 = c1.annotate(keys)
        assert res1[("KRAS", "G12D", "missense_variant", "GRCh37")] == "oncokb"
        assert c1.unique_variants_queried == 2
        post_calls_1 = [c for c in calls if c[0].endswith("byProteinChange")]
        assert len(post_calls_1) == 2  # batch_size=1 splits 2 unique keys into 2 requests
        db_bytes = c1.cache_db_path.read_bytes()
        assert b"my-super-secret" not in db_bytes
    finally:
        c1.close()

    # Second run against same dataVersion hits SQLite cache for the annotated variants.
    calls2 = []
    req2 = _fake_oncokb_requester(CGL, {}, info_version="v7.7", calls=calls2)
    c2 = oncokb.OncoKBClient(tmp_path, token="my-super-secret", requester=req2)
    try:
        res2 = c2.annotate(keys)
        assert res2 == res1
        assert c2.unique_variants_queried == 0 and c2.cache_hits == 2
        # Preflight check on empty missing list still runs exactly 1 auth verification call,
        # and does not hit /utils/cancerGeneList again thanks to cached index.
        assert not any(c[0].endswith("cancerGeneList") for c in calls2)
        assert len([c for c in calls2 if c[0].endswith("byProteinChange")]) == 1
    finally:
        c2.close()

    # New OncoKB version creates separate cache namespace and changes provenance.
    req3 = _fake_oncokb_requester(
        CGL,
        {("KRAS", "G12D"): "Oncogenic", ("EGFR", "L858R"): "Likely Neutral"},
        info_version="v7.8",
    )
    c3 = oncokb.OncoKBClient(tmp_path, token="my-super-secret", requester=req3)
    try:
        res3 = c3.annotate(keys)
        assert res3[("EGFR", "L858R", "missense_variant", "GRCh37")] == "neutral"
        assert c3.provenance() != c1.provenance()
    finally:
        c3.close()


def test_oncokb_retries_on_429_fails_fast_on_401_and_redacts_token(monkeypatch):
    """429 retries with Retry-After, 401 fails without retry, and errors never leak secrets."""
    secret = "secret-oncokb-uuid-value"
    attempts = []
    sleeps = []

    def fake_urlopen(req, timeout=60.0):
        attempts.append(req.full_url)
        if len(attempts) == 1:
            raise HTTPError(
                req.full_url, 429, "Too Many Requests", {"Retry-After": "2"}, io.BytesIO(b"")
            )
        if len(attempts) == 2:
            raise TimeoutError(f"timed out with token {secret}")
        raise HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))

    monkeypatch.setattr(oncokb, "urlopen", fake_urlopen)
    with pytest.raises(oncokb.OncoKBAuthError, match="HTTP 401"):
        oncokb.request_json(
            "https://www.oncokb.org/api/v1/annotate/mutations/byProteinChange",
            payload=[],
            token=secret,
            attempts=4,
            sleep=sleeps.append,
        )
    assert len(attempts) == 3  # retried 429 + TimeoutError, then stopped immediately on 401
    assert sleeps[0] >= 2.0

    # Timeout exhaustion must also redact token from error text.
    def always_timeout(req, timeout=60.0):
        raise TimeoutError(f"bad header {secret}")

    monkeypatch.setattr(oncokb, "urlopen", always_timeout)
    with pytest.raises(oncokb.OncoKBError) as exc_info:
        oncokb.request_json(
            "https://www.oncokb.org/api/v1/info",
            token=secret,
            attempts=2,
            sleep=lambda _: None,
        )
    assert secret not in str(exc_info.value)
    assert "[REDACTED]" in str(exc_info.value)


def test_missing_token_fails_closed_unless_hotspot_only(tmp_path, monkeypatch):
    """Missing ONCOKB_API_TOKEN fails non-hotspot build; --hotspot-only runs and marks build."""
    monkeypatch.delenv("ONCOKB_API_TOKEN", raising=False)
    # Restore real oncokb.load_client so autouse stub doesn't mask missing-token check.
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
        # EGFR wrongly marked TSG in Bailey, ROS1 absent from OncoKB CGL -> fallback TSG.
        tumor_suppressors=frozenset({"EGFR", "TP53", "ROS1"}),
    )
    syn_dir = tmp_path / "syn"
    syn_dir.mkdir()
    inputs, mapping, selection = synthetic(syn_dir)
    # Put non-annotated empty-HGVSp Splice_Site mutations in EGFR, TP53, ROS1 across AA.
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
    req = _fake_oncokb_requester(cgl, {})
    client = oncokb.OncoKBClient(tmp_path, token="t", requester=req)
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
        # EGFR is ONCOGENE in OncoKB -> truncating fallback blocked; TP53 & ROS1 count.
        assert category(res, "EGFR", "AA", POINT_MUTATION)["n_events"] == 0
        assert category(res, "TP53", "AA", POINT_MUTATION)["n_events"] == 1
        assert category(res, "ROS1", "AA", POINT_MUTATION)["n_events"] == 1
    finally:
        client.close()


def test_release_assets_never_contain_per_variant_oncokb_data_and_version_changes_release_id(
    tmp_path, monkeypatch
):
    """Manifest/gene artifacts omit per-variant OncoKB data and track OncoKB version changes."""
    from test_alterations import build_fixture, rich_inputs

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
        assert manifest["build"]["hotspot_only"] is False
        lihc_pm = tp53["cancer_types"][0]["alterations"]["categories"][POINT_MUTATION]
        assert lihc_pm["sources"] == {"oncokb": 15, "hotspot": 0}
        releases[ver] = manifest["release_id"]
        # Confirm SQLite cache stays only under .cache, never in out/.
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

    # Version bump -> changed=true, deployable=true.
    monkeypatch.setenv("INPUT_HOTSPOT_ONLY", "false")
    monkeypatch.setitem(checker["main"].__globals__, "current_oncokb_version", lambda: "v7.8")
    out1 = tmp_path / "out1"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out1))
    checker["main"]()
    vals1 = dict(line.split("=", 1) for line in out1.read_text().splitlines())
    assert vals1["changed"] == "true" and vals1["deployable"] == "true"

    # Hotspot-only -> changed=true, deployable=false.
    monkeypatch.setenv("INPUT_HOTSPOT_ONLY", "true")
    out2 = tmp_path / "out2"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out2))
    checker["main"]()
    vals2 = dict(line.split("=", 1) for line in out2.read_text().splitlines())
    assert vals2["changed"] == "true" and vals2["deployable"] == "false"
