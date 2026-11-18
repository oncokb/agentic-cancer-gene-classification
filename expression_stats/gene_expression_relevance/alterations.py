"""Alteration-type enrichment (Point Mutation, Copy Number Alteration, Structural Variant).

Detail only: nothing here reads or writes expression statistics. Each gene x cohort x
category receives an explicit status (enriched / not_enriched / insufficient_data /
not_testable) from a one-sided Fisher test of patient-level event frequency in the
cohort versus the other cohorts, with BH across cohorts per gene per category.

Inputs are the pinned datahub files of the same 32 studies. Each raw file is parsed once
into a compact, selection-independent summary cached by LFS object id, so raw downloads
can be deleted immediately (CI disk) and later builds skip the download entirely.
"""

import hashlib
import json
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import hypergeom

from . import (
    ALTERATION_CATEGORIES,
    ALTERATION_PARAMS,
    COPY_NUMBER_ALTERATION,
    POINT_MUTATION,
    STRUCTURAL_VARIANT,
)
from . import fetch as fetching
from . import oncokb as oncokb_client
from .stats import bh

# Bump when a parser's output changes; summaries are cached per parser version.
PARSER_VERSION = "3"
FILES = {"mutation": "data_mutations.txt", "cna": "data_cna.txt", "sv": "data_sv.txt"}
CASE_LISTS = {"mutation": "cases_sequenced.txt", "cna": "cases_cna.txt", "sv": "cases_sv.txt"}
# Coding consequence classes; their per-sample row count is the hypermutator burden.
NONSYNONYMOUS = (
    "Missense_Mutation",
    "Nonsense_Mutation",
    "Nonstop_Mutation",
    "Splice_Site",
    "Translation_Start_Site",
    "Frame_Shift_Del",
    "Frame_Shift_Ins",
    "In_Frame_Del",
    "In_Frame_Ins",
)
CLASS = {name: i for i, name in enumerate(NONSYNONYMOUS)}
MISSENSE = CLASS["Missense_Mutation"]
TRUNCATING = tuple(
    CLASS[name]
    for name in ("Nonsense_Mutation", "Frame_Shift_Del", "Frame_Shift_Ins", "Splice_Site")
)
INFRAME = (CLASS["In_Frame_Del"], CLASS["In_Frame_Ins"])
CNA_MISSING = -128
MUTATION_COLUMNS = [
    "Hugo_Symbol",
    "Entrez_Gene_Id",
    "Variant_Classification",
    "Tumor_Sample_Barcode",
    "Transcript_ID",
    "HGVSp_Short",
]
OPTIONAL_MUTATION_COLUMNS = set(MUTATION_COLUMNS) | {"Consequence", "NCBI_Build"}
POSITION = re.compile(r"^p\.(?:Sec|[A-Z*]|(?=\d+_))(\d+)(?:[A-Z*]|_|del|ins|dup)")
PRIMARY_TYPES = ("01", "03")
STATUSES = ("enriched", "not_enriched", "insufficient_data", "not_testable")
DIRECTIONS = ("amp", "del")
NOT_CNA_DRIVER = "not a copy-number driver"
ROLE_NOTE = {
    ("amp",): "not called: oncogene role allows amplification only",
    ("del",): "not called: tumor suppressor role allows deep deletion only",
}


# ---------------------------------------------------------------- parsing (per file)


def _leading_comments(path):
    count = 0
    with Path(path).open("rb") as stream:
        for line in stream:
            if not line.startswith(b"#"):
                break
            count += 1
    return count


def _table(values):
    """Factorize strings to (codes, table) with stable, sorted tables."""
    table, codes = np.unique(np.asarray(values, dtype=str), return_inverse=True)
    return codes.astype(np.int32), table


def parse_mutations(path, chunksize=250_000):
    """Nonsynonymous rows of every gene, compactly: enough for burden, OncoKB and fallback."""
    parts = []
    reader = pd.read_csv(
        path,
        sep="\t",
        usecols=lambda column: column in OPTIONAL_MUTATION_COLUMNS,
        dtype=str,
        keep_default_na=False,
        skiprows=_leading_comments(path),
        chunksize=chunksize,
        quoting=3,
    )
    for chunk in reader:
        chunk = chunk[chunk["Variant_Classification"].isin(CLASS)]
        if len(chunk):
            parts.append(chunk)
    all_columns = MUTATION_COLUMNS + ["Consequence", "NCBI_Build"]
    frame = (
        pd.concat(parts, ignore_index=True)
        if parts
        else pd.DataFrame({column: pd.Series(dtype=str) for column in all_columns})
    )
    samples, sample_table = _table(frame["Tumor_Sample_Barcode"].str.slice(0, 15))
    symbols, symbol_table = _table(frame["Hugo_Symbol"].str.upper())
    transcripts, transcript_table = _table(frame["Transcript_ID"])
    classes = frame["Variant_Classification"].map(CLASS).to_numpy(dtype=np.int8)
    position = (
        frame["HGVSp_Short"].str.extract(POSITION.pattern, expand=False).fillna("-1").astype(int)
    )
    hgvsp, hgvsp_table = _table(frame["HGVSp_Short"])
    consequences, consequence_table = _table(
        frame["Consequence"] if "Consequence" in frame else [""] * len(frame)
    )
    builds, build_table = _table(
        frame["NCBI_Build"] if "NCBI_Build" in frame else ["GRCh37"] * len(frame)
    )
    entrez = pd.to_numeric(frame["Entrez_Gene_Id"], errors="coerce").fillna(0).astype(np.int64)
    return dict(
        sample=samples,
        sample_table=sample_table,
        symbol=symbols,
        symbol_table=symbol_table,
        entrez=entrez.to_numpy(),
        cls=classes,
        transcript=transcripts,
        transcript_table=transcript_table,
        position=position.to_numpy(dtype=np.int32),
        hgvsp=hgvsp,
        hgvsp_table=hgvsp_table,
        consequence=consequences,
        consequence_table=consequence_table,
        build=builds,
        build_table=build_table,
    )


def parse_cna(path):
    """GISTIC discrete calls as int8 (missing = -128), all samples and genes."""
    frame = pd.read_csv(path, sep="\t", dtype={"Hugo_Symbol": str, "Entrez_Gene_Id": str})
    symbols = frame.pop("Hugo_Symbol").fillna("").str.upper().to_numpy(dtype=str)
    entrez = pd.to_numeric(frame.pop("Entrez_Gene_Id"), errors="coerce").fillna(0)
    frame = frame.drop(columns=[c for c in frame.columns if c == "Cytoband"])
    values = frame.apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)
    calls = np.where(np.isfinite(values), np.rint(values), CNA_MISSING).astype(np.int8)
    return dict(
        samples=np.asarray([c[:15] for c in frame.columns], dtype=str),
        symbol=symbols,
        entrez=entrez.astype(np.int64).to_numpy(),
        calls=calls,
    )


def parse_sv(path):
    frame = pd.read_csv(path, sep="\t", dtype=str, keep_default_na=False, quoting=3)
    status = frame["SV_Status"] if "SV_Status" in frame else pd.Series([""] * len(frame))
    keep = status.str.upper() != "GERMLINE"
    frame = frame[keep]
    return dict(
        sample=frame["Sample_Id"].str.slice(0, 15).to_numpy(dtype=str),
        site1=frame.get("Site1_Hugo_Symbol", pd.Series([""] * len(frame)))
        .str.upper()
        .to_numpy(dtype=str),
        site2=frame.get("Site2_Hugo_Symbol", pd.Series([""] * len(frame)))
        .str.upper()
        .to_numpy(dtype=str),
    )


PARSERS = {"mutation": parse_mutations, "cna": parse_cna, "sv": parse_sv}


def parse_case_list(text):
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "case_list_ids":
            return {s.strip()[:15] for s in re.split(r"[\t ]+", value.strip()) if s.strip()}
    raise ValueError("Case list without case_list_ids")


# ---------------------------------------------------------------- download + summary cache


def _save(target, data):
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".npz", delete=False) as handle:
        np.savez_compressed(handle, **data)
    Path(handle.name).replace(target)


def summary(commit, cache, study, kind, *, delete_downloads=False, remaining=None):
    """(oid, parsed summary) for one pinned study file, downloading only on a cache miss."""
    if remaining is not None:
        remaining()
    relative = f"public/{study}/{FILES[kind]}"
    locate_kw = {"remaining": remaining} if remaining is not None else {}
    oid, size, inline = fetching.locate(commit, relative, **locate_kw)
    target = Path(cache) / "alterations" / f"p{PARSER_VERSION}" / kind / f"{oid}.npz"
    if target.exists():
        try:
            with np.load(target, allow_pickle=False) as stored:
                return oid, {key: stored[key] for key in stored.files}
        except (OSError, ValueError, KeyError):
            target.unlink(missing_ok=True)
    if inline is None:
        fetch_kw = {"remaining": remaining} if remaining is not None else {}
        path = fetching.fetch_lfs(
            commit, cache, relative, oid, size, f"{study}/{FILES[kind]}", **fetch_kw
        )
    else:
        path = Path(cache) / "objects" / oid
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(inline)
    try:
        data = PARSERS[kind](path)
    finally:
        if delete_downloads:
            path.unlink(missing_ok=True)
    _save(target, data)
    return oid, data


def case_list(commit, cache, study, kind, *, remaining=None):
    target = Path(cache) / "case_lists" / commit / study / CASE_LISTS[kind]
    if not target.exists():
        if remaining is not None:
            remaining()
        pin_kw = {"remaining": remaining} if remaining is not None else {}
        data = fetching.pinned_bytes(
            commit, f"public/{study}/case_lists/{CASE_LISTS[kind]}", **pin_kw
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    data = target.read_bytes()
    return hashlib.sha256(data).hexdigest(), parse_case_list(data.decode())


def load_inputs(commit, cache, studies, *, workers=4, delete_downloads=False, remaining=None):
    """Per-study summaries and case lists, plus a fingerprint of every input object."""
    from concurrent.futures import FIRST_COMPLETED, wait

    jobs = [(study, kind) for study in studies for kind in FILES]

    def run(job):
        if remaining is not None:
            remaining()
        study, kind = job
        oid, data = summary(
            commit, cache, study, kind, delete_downloads=delete_downloads, remaining=remaining
        )
        list_sha, cases = case_list(commit, cache, study, kind, remaining=remaining)
        return job, oid, list_sha, data, cases

    completed = {}
    max_workers = max(1, int(workers))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        pending = set()
        job_iter = iter(jobs)

        def fill_pool():
            while len(pending) < max_workers:
                if remaining is not None:
                    remaining()
                try:
                    job = next(job_iter)
                except StopIteration:
                    break
                pending.add(pool.submit(run, job))

        try:
            fill_pool()
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    job, oid, list_sha, data, cases = future.result()
                    completed[job] = (oid, list_sha, data, cases)
                if remaining is not None:
                    remaining()
                fill_pool()
        except Exception:
            for future in pending:
                future.cancel()
            raise

    inputs, provenance = {}, {}
    for study, kind in jobs:
        oid, list_sha, data, cases = completed[(study, kind)]
        inputs.setdefault(study, {})[kind] = dict(data=data, cases=cases)
        provenance.setdefault(study, {})[kind] = dict(
            file=FILES[kind], sha256=oid, case_list=CASE_LISTS[kind], case_list_sha256=list_sha
        )
    digest = hashlib.sha256(
        json.dumps(provenance, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return inputs, provenance, digest


# ---------------------------------------------------------------- gene resolution


class Genes:
    """Map source (symbol, Entrez) pairs onto the build's gene rows."""

    def __init__(self, ids, catalog, aliases):
        self.ids = list(ids)
        self.row = {entrez: i for i, entrez in enumerate(self.ids)}
        self.by_symbol = {symbol: entrez for entrez, symbol in catalog.items()}
        self.valid = set(catalog)
        self.aliases = aliases

    def entrez(self, symbol, entrez=0):
        """Resolve against the full catalog: Entrez first, then symbol, then alias."""
        if int(entrez) in self.valid:
            return int(entrez)
        symbol = str(symbol).upper()
        if symbol in self.by_symbol:
            return self.by_symbol[symbol]
        alias = self.aliases.get(symbol)
        return self.by_symbol.get(alias) if alias else None

    def rows(self, symbols, entrez=None):
        """Row index per source record (-1 when unresolved or outside this build)."""
        symbols = np.asarray(symbols, dtype=str)
        entrez = np.zeros(len(symbols), dtype=np.int64) if entrez is None else np.asarray(entrez)
        if not len(symbols):
            return np.zeros(0, dtype=np.int64)
        table, codes = np.unique(symbols, return_inverse=True)
        keys = (codes.astype(np.int64) << 32) | (entrez.astype(np.int64) & 0xFFFFFFFF)
        unique, inverse = np.unique(keys, return_inverse=True)
        resolved = np.array(
            [
                self.row.get(self.entrez(table[key >> 32], key & 0xFFFFFFFF), -1)
                for key in unique.tolist()
            ],
            dtype=np.int64,
        )
        return resolved[inverse.reshape(-1)]


# ---------------------------------------------------------------- event classifiers


def classify_point_mutations(
    records,
    retained,
    tsg_rows,
    params=ALTERATION_PARAMS,
    *,
    driver_rows=None,
    oncokb=None,
    row_symbols=None,
    excluded_counts=None,
    non_dominant_counts=None,
):
    """Classify OncoKB-first point mutations with pan-cancer position-hotspot + TSG fallback.

    Returns (events, source_by_event) where events is set[(gene row, sample)] and
    source_by_event maps each (gene row, sample) to 'oncokb' or 'hotspot' (OncoKB takes
    precedence if a tumor has both). OncoKB 'neutral', 'inconclusive', and
    'predicted_oncogenic' statuses are excluded and never fall back. Missense and
    in-frame indels require >= missense_min_recurrence (10) retained tumors pan-cancer
    at the same (gene, protein position) on the gene's dominant (most frequent)
    transcript, counting all such mutations regardless of OncoKB verdict; position -1
    never groups and rows on non-dominant transcripts are excluded from grouping and
    reported in non_dominant_counts.
    """
    rows_arr = np.asarray(records["row"])
    samples_arr = np.asarray(records["sample"])
    keep = (rows_arr >= 0) & np.isin(samples_arr, list(retained))
    if driver_rows is not None:
        keep &= np.isin(rows_arr, list(driver_rows))
    if not keep.any():
        return set(), {}

    kept_rows = rows_arr[keep]
    kept_samples = samples_arr[keep]
    kept_cls = np.asarray(records["cls"])[keep]
    kept_transcript = np.asarray(records["transcript"])[keep]
    kept_position = np.asarray(records["position"])[keep]
    kept_hgvsp = np.asarray(records["hgvsp"])[keep]
    n_kept = len(kept_rows)
    kept_conseq = (
        np.asarray(records["consequence"])[keep]
        if "consequence" in records
        else np.full(n_kept, "", dtype=object)
    )
    kept_build = (
        np.asarray(records["build"])[keep]
        if "build" in records
        else np.full(n_kept, "GRCh37", dtype=object)
    )

    verdicts = np.full(n_kept, "unknown", dtype=object)
    if oncokb is not None:
        if row_symbols is None:
            raise ValueError("row_symbols is required when oncokb annotation is enabled")
        resolved_symbols = [oncokb.index.resolve_symbol(sym) for sym in row_symbols]
        row_has_okb = np.array(
            [resolved_symbols[int(r)] is not None for r in range(len(resolved_symbols))],
            dtype=bool,
        )
        candidate_indices = np.flatnonzero(row_has_okb[kept_rows])
        query_keys = {}
        for idx in candidate_indices:
            alt = oncokb_client.normalize_alteration(kept_hgvsp[idx])
            if not alt:
                continue
            sym = resolved_symbols[int(kept_rows[idx])]
            conseq = oncokb_client.normalize_consequence(
                kept_conseq[idx], NONSYNONYMOUS[int(kept_cls[idx])]
            )
            genome = oncokb_client.normalize_reference_genome(kept_build[idx])
            query_keys[int(idx)] = (sym, alt, conseq, genome)
        annotations = oncokb.annotate(query_keys.values())
        for idx, key in query_keys.items():
            verdicts[idx] = annotations[key]

    frame = pd.DataFrame(
        dict(
            row=kept_rows,
            sample=kept_samples,
            cls=kept_cls,
            transcript=kept_transcript,
            position=kept_position,
            hgvsp=kept_hgvsp,
            verdict=verdicts,
        )
    )
    fallback = frame[frame["verdict"] == "unknown"]

    position_classes = (MISSENSE, *INFRAME)
    pos_rows = frame[frame["cls"].isin(position_classes)]
    if not pos_rows.empty:
        tx_counts = (
            pos_rows.groupby(["row", "transcript"], sort=False)
            .size()
            .rename("tx_count")
            .reset_index()
        )
        tx_counts["_empty_tx"] = tx_counts["transcript"] == ""
        tx_counts = tx_counts.sort_values(
            ["row", "_empty_tx", "tx_count", "transcript"],
            ascending=[True, True, False, True],
            kind="mergesort",
        )
        dominant_tx = tx_counts.drop_duplicates("row", keep="first").set_index("row")["transcript"]
        dom_for_pos = pos_rows["row"].map(dominant_tx)
        is_dominant = pos_rows["transcript"].to_numpy() == dom_for_pos.to_numpy()
        non_dom = pos_rows[~is_dominant]
        if non_dominant_counts is not None and not non_dom.empty:
            for r, cnt in non_dom.groupby("row", sort=True).size().items():
                key = row_symbols[int(r)] if row_symbols is not None else int(r)
                non_dominant_counts[key] = int(non_dominant_counts.get(key, 0)) + int(cnt)
        dominant_pos = pos_rows[is_dominant]
    else:
        dominant_tx = pd.Series(dtype=object)
        dominant_pos = pos_rows

    # Recurrence counts ALL retained missense/in-frame tumors at (gene, position) on
    # the gene's dominant transcript regardless of OncoKB verdict; position -1 never groups.
    hotspot_pool = dominant_pos[dominant_pos["position"] >= 0]
    recurrent = hotspot_pool.groupby(["row", "position"])["sample"].nunique()
    recurrent = recurrent[recurrent >= params["missense_min_recurrence"]]

    fallback_pos = fallback[fallback["cls"].isin(position_classes) & (fallback["position"] >= 0)]
    if not fallback_pos.empty and not dominant_tx.empty:
        fallback_pos = fallback_pos[
            fallback_pos["transcript"] == fallback_pos["row"].map(dominant_tx)
        ]
    position_hits = fallback_pos.join(
        recurrent.rename("tumors"), on=["row", "position"], how="inner"
    )

    truncating_hits = fallback[
        fallback["cls"].isin(TRUNCATING) & fallback["row"].isin(list(tsg_rows))
    ]

    if excluded_counts is not None:
        for status_key in oncokb_client.EXCLUDED_STATUS_KEYS:
            sub = frame[frame["verdict"] == status_key]
            excluded_counts[status_key] = len(
                set(zip(sub["row"].tolist(), sub["sample"].tolist(), strict=True))
            )

    source_by_event = {}
    for part in (position_hits, truncating_hits):
        for r, s in zip(part["row"].tolist(), part["sample"].tolist(), strict=True):
            source_by_event[(int(r), s)] = "hotspot"
    oncokb_hits = frame[frame["verdict"] == "oncokb"]
    for r, s in zip(oncokb_hits["row"].tolist(), oncokb_hits["sample"].tolist(), strict=True):
        source_by_event[(int(r), s)] = "oncokb"
    return set(source_by_event), source_by_event


def point_mutation_events(
    records,
    retained,
    driver_rows,
    tsg_rows,
    params=ALTERATION_PARAMS,
    *,
    oncokb=None,
    row_symbols=None,
):
    """Putative-driver point mutations per (gene row, sample)."""
    events, _ = classify_point_mutations(
        records,
        retained,
        tsg_rows,
        params,
        driver_rows=driver_rows,
        oncokb=oncokb,
        row_symbols=row_symbols,
    )
    return events


def cna_counts(calls, gene_rows, n_genes):
    """Per gene row: (amp, del, measured) patient counts; duplicate rows combine per sample."""
    amp = np.zeros(n_genes, dtype=np.int64)
    deletion = np.zeros(n_genes, dtype=np.int64)
    measured = np.zeros(n_genes, dtype=np.int64)
    keep = gene_rows >= 0
    if not keep.any() or calls.shape[1] == 0:
        return amp, deletion, measured
    rows, calls = gene_rows[keep], calls[keep]
    order = np.argsort(rows, kind="stable")
    rows, calls = rows[order], calls[order]
    unique, starts = np.unique(rows, return_index=True)
    amp[unique] = np.logical_or.reduceat(calls == 2, starts, axis=0).sum(axis=1)
    deletion[unique] = np.logical_or.reduceat(calls == -2, starts, axis=0).sum(axis=1)
    measured[unique] = np.logical_or.reduceat(calls != CNA_MISSING, starts, axis=0).sum(axis=1)
    return amp, deletion, measured


# ---------------------------------------------------------------- statistics


def fisher(events, profiled):
    """One-sided Fisher (hypergeometric upper tail) of each column versus the rest.

    events, profiled: (genes, cohorts) integer arrays. Returns a dict of arrays; undefined
    tests (empty target or rest) have p=NaN, which BH treats as p=1.
    """
    events = np.asarray(events, dtype=np.int64)
    profiled = np.broadcast_to(np.asarray(profiled, dtype=np.int64), events.shape)
    total = profiled.sum(axis=-1, keepdims=True)
    hits = events.sum(axis=-1, keepdims=True)
    rest_events, rest_profiled = hits - events, total - profiled
    with np.errstate(invalid="ignore", divide="ignore"):
        frequency = np.where(profiled > 0, events / profiled, np.nan)
        rest_frequency = np.where(rest_profiled > 0, rest_events / rest_profiled, np.nan)
        ratio = np.where(rest_frequency > 0, frequency / rest_frequency, np.nan)
    defined = (profiled > 0) & (rest_profiled > 0)
    total_b, hits_b = np.broadcast_to(total, events.shape), np.broadcast_to(hits, events.shape)
    p = np.where(defined, hypergeom.sf(events - 1, total_b, hits_b, profiled), np.nan)
    return dict(
        n_events=events,
        n_profiled=profiled,
        frequency=frequency,
        rest_n_events=rest_events,
        rest_n_profiled=rest_profiled,
        rest_frequency=rest_frequency,
        ratio=ratio,
        p_value=np.minimum(p, 1.0),
    )


def gate(result, i, j, category, params=ALTERATION_PARAMS):
    """Enrichment gates for one comparison; returns (passed, failed-gate descriptions)."""
    rules = params["gates"][category]
    n, freq = int(result["n_events"][i, j]), result["frequency"][i, j]
    rest_freq, ratio, q = (
        result["rest_frequency"][i, j],
        result["ratio"][i, j],
        result["q_value"][i, j],
    )
    failed = []
    if not q < params["fdr"]:
        failed.append(f"q={_fmt(q)} >= {params['fdr']}")
    # A zero rest frequency with target events is an unbounded ratio and passes.
    unbounded = rest_freq == 0 and n > 0
    if not (unbounded or ratio >= rules["min_ratio"]):
        failed.append(f"ratio={_fmt(ratio)} < {rules['min_ratio']}")
    if n < rules["min_events"]:
        failed.append(f"events={n} < {rules['min_events']}")
    if not freq >= rules["min_frequency"]:
        failed.append(f"frequency={_fmt(freq)} < {rules['min_frequency']}")
    return not failed, failed


def _fmt(value):
    return "NA" if value is None or not np.isfinite(value) else f"{value:.3g}"


def _num(value, kind=float):
    return kind(value) if np.isfinite(value) else None


def _stats(result, i, j):
    return dict(
        n_events=int(result["n_events"][i, j]),
        frequency=_num(result["frequency"][i, j]),
        rest_n_events=int(result["rest_n_events"][i, j]),
        rest_n_profiled=int(result["rest_n_profiled"][i, j]),
        rest_frequency=_num(result["rest_frequency"][i, j]),
        ratio=_num(result["ratio"][i, j]),
        p_value=_num(result["p_value"][i, j]),
        q_value=_num(result["q_value"][i, j]),
    )


EMPTY = dict(
    n_events=None,
    frequency=None,
    rest_n_events=None,
    rest_n_profiled=None,
    rest_frequency=None,
    ratio=None,
    p_value=None,
    q_value=None,
)


def untestable(reason, n_profiled=None, cna=False, point_mutation=False):
    entry = dict(status="not_testable", reason=reason, n_profiled=n_profiled, **EMPTY)
    entry["direction"] = None
    if point_mutation:
        entry["sources"] = None
    if cna:
        entry.update(enriched_directions=[], allowed_directions=[], amp=None, **{"del": None})
    return entry


# ---------------------------------------------------------------- per-build aggregation


class Alterations:
    """All alteration results for one build, addressable by (gene row, cohort index)."""

    def __init__(
        self,
        codes,
        results,
        reasons,
        gene_reasons,
        summary,
        params=ALTERATION_PARAMS,
        cna_allowed=None,
        pm_sources=None,
    ):
        self.codes = codes
        self.results = results  # category -> fisher dict (CNA: {"amp": ..., "del": ...})
        self.reasons = reasons  # category -> {cohort index: not_testable reason}
        self.gene_reasons = gene_reasons  # category -> callable(i, j) -> reason or None
        self.summary = summary
        self.params = params
        # (genes, 2) booleans: may amp / del be called enriched for this gene row.
        self.cna_allowed = cna_allowed
        # Optional tuple (pm_oncokb_events, pm_hotspot_events) of shape (genes, cohorts).
        self.pm_sources = pm_sources

    def _cna_directions(self, i):
        if self.cna_allowed is None:
            return DIRECTIONS
        return tuple(d for d, ok in zip(DIRECTIONS, self.cna_allowed[i], strict=True) if ok)

    def _category(self, category, i, j):
        reason = self.reasons[category].get(j) or self.gene_reasons[category](i, j)
        cna = category == COPY_NUMBER_ALTERATION
        pm = category == POINT_MUTATION
        result = self.results[category]["amp"] if cna else self.results[category]
        n_profiled = int(result["n_profiled"][i, j])
        if reason:
            return untestable(reason, n_profiled, cna, point_mutation=pm)
        minimum = self.params["min_profiled"]
        if not cna:
            entry = dict(status=None, reason=None, n_profiled=n_profiled, **_stats(result, i, j))
            entry["direction"] = None
            if pm and self.pm_sources is not None:
                entry["sources"] = dict(
                    oncokb=int(self.pm_sources[0][i, j]),
                    hotspot=int(self.pm_sources[1][i, j]),
                )
            passed, failed = gate(result, i, j, category, self.params)
            rules = self.params["gates"][category]
            if n_profiled < minimum:
                entry.update(
                    status="insufficient_data",
                    reason=f"{n_profiled} profiled patients < {minimum}",
                )
            elif passed:
                entry["status"] = "enriched"
            elif category == STRUCTURAL_VARIANT and entry["n_events"] < rules["min_events"]:
                entry.update(
                    status="insufficient_data",
                    reason=f"{entry['n_events']} fusion events < {rules['min_events']}; "
                    "too rare to call not enriched",
                )
            else:
                entry.update(status="not_enriched", reason="; ".join(failed))
            return entry
        allowed = self._cna_directions(i)
        components, outcomes = {}, {}
        for direction in DIRECTIONS:
            part = self.results[category][direction]
            passed, failed = gate(part, i, j, category, self.params)
            if direction not in allowed:
                # Statistics stay visible; the role forbids calling this direction.
                passed, failed = False, [ROLE_NOTE[allowed]]
            components[direction] = dict(**_stats(part, i, j), enriched=passed)
            outcomes[direction] = failed
        enriched = [d for d in DIRECTIONS if components[d]["enriched"]]
        if enriched:
            lead = enriched[0]
        elif len(allowed) == 1:
            lead = allowed[0]
        else:
            p_amp = components["amp"]["p_value"]
            p_del = components["del"]["p_value"]
            lead = "del" if p_del is not None and (p_amp is None or p_del < p_amp) else "amp"
        top = {k: v for k, v in components[lead].items() if k != "enriched"}
        entry = dict(status=None, reason=None, n_profiled=n_profiled, **top)
        entry.update(direction=lead, enriched_directions=enriched, amp=components["amp"])
        entry["del"] = components["del"]
        entry["allowed_directions"] = list(allowed)
        if n_profiled < minimum:
            entry.update(
                status="insufficient_data",
                reason=f"{n_profiled} profiled patients < {minimum}",
                enriched_directions=[],
            )
            for direction in ("amp", "del"):
                entry[direction]["enriched"] = False
        elif enriched:
            entry["status"] = "enriched"
        else:
            entry.update(
                status="not_enriched",
                reason=f"amp: {'; '.join(outcomes['amp'])} | del: {'; '.join(outcomes['del'])}",
            )
        return entry

    def record(self, i, j):
        categories = {
            category: self._category(category, i, j) for category in ALTERATION_CATEGORIES
        }
        return dict(
            alteration_types_enriched=[
                c for c in ALTERATION_CATEGORIES if categories[c]["status"] == "enriched"
            ],
            categories=categories,
        )


def _sample_types(samples):
    return sorted({s[13:15] for s in samples if len(s) >= 15})


def _cohort_reason(label, listed, selected_profiled):
    if selected_profiled:
        return None
    if not listed:
        return f"no {label} profile in this study"
    types = _sample_types(listed)
    if not set(types) & set(PRIMARY_TYPES):
        kind = " (metastatic)" if types == ["06"] else ""
        return (
            f"{label} data cover only non-primary samples (TCGA sample type "
            f"{', '.join(types)}{kind}); no selected primary tumor is profiled"
        )
    return f"no selected primary tumor is in the {label} case list"


def compute(
    inputs,
    mapping,
    selection,
    ids,
    catalog,
    aliases,
    driver=None,
    params=ALTERATION_PARAMS,
    *,
    cna_driver,
    oncokb=None,
):
    """Build Alterations for gene rows `ids` (Entrez) over the cohorts in `mapping` order.

    driver: fallback mutation DriverSource (used for TSG fallback roles when OncoKB
    does not cover a gene or in hotspot-only review mode); cna_driver: CnaDriverSource;
    oncokb: optional live OncoKBClient for OncoKB-first point-mutation calling.
    """
    codes = [item["oncotree_code"] for item in mapping]
    studies = [item["study_id"] for item in mapping]
    genes = Genes(ids, catalog, aliases)
    row_symbols = [catalog[entrez] for entrez in ids]
    n_genes, n_codes = len(ids), len(codes)
    reasons = {category: {} for category in ALTERATION_CATEGORIES}
    cohorts = {}
    # Patient-level counts rely on one selected sample per patient across all cohorts.
    patients = [sample_id[:12] for study in studies for sample_id in selection[study]]
    if len(patients) != len(set(patients)):
        raise ValueError("Sample selection must contain at most one sample per patient")

    # Point Mutation ------------------------------------------------------------------
    fallback_tsg_symbols = set(driver.tumor_suppressors) if driver is not None else set()
    if oncokb is not None:
        tsg_symbols = set(oncokb.index.tsg_genes) | {
            symbol
            for symbol in fallback_tsg_symbols
            if oncokb.index.resolve_symbol(symbol) is None
        }
    else:
        tsg_symbols = fallback_tsg_symbols
    tsg_rows = set()
    for symbol in tsg_symbols:
        entrez = genes.entrez(symbol)
        if entrez in genes.row:
            tsg_rows.add(genes.row[entrez])
    record_keys = (
        "row",
        "sample",
        "cls",
        "transcript",
        "position",
        "hgvsp",
        "consequence",
        "build",
    )
    records = {k: [] for k in record_keys}
    retained, profiled_counts = set(), np.zeros(n_codes, dtype=np.int64)
    sample_cohort = {}
    for j, study in enumerate(studies):
        data, cases = inputs[study]["mutation"]["data"], inputs[study]["mutation"]["cases"]
        selected = set(selection[study])
        profiled = selected & cases
        sample_ids = data["sample_table"][data["sample"]]
        counts = np.bincount(data["sample"], minlength=len(data["sample_table"]))
        burden = dict(zip(data["sample_table"].tolist(), counts.tolist(), strict=True))
        limit = params["hypermutator_max_nonsynonymous"]
        kept = {s for s in profiled if burden.get(s, 0) <= limit}
        retained |= kept
        profiled_counts[j] = len(kept)
        for s in kept:
            sample_cohort[s] = j
        reasons[POINT_MUTATION][j] = _cohort_reason("mutation", cases, profiled) or (
            None if kept else "all profiled patients exceed the hypermutator cutoff"
        )
        cohorts.setdefault(codes[j], {})[POINT_MUTATION] = dict(
            selected=len(selected),
            profiled=len(profiled),
            hypermutators_excluded=len(profiled) - len(kept),
            n_profiled=len(kept),
        )
        rows = genes.rows(data["symbol_table"][data["symbol"]], data["entrez"])
        mask = (rows >= 0) & np.isin(sample_ids, list(kept))
        records["row"].append(rows[mask])
        records["sample"].append(sample_ids[mask])
        records["cls"].append(data["cls"][mask])
        records["transcript"].append(data["transcript_table"][data["transcript"]][mask])
        records["position"].append(data["position"][mask])
        records["hgvsp"].append(data["hgvsp_table"][data["hgvsp"]][mask])
        if "consequence" in data and "consequence_table" in data:
            records["consequence"].append(data["consequence_table"][data["consequence"]][mask])
        else:
            records["consequence"].append(np.full(int(mask.sum()), "", dtype=str))
        if "build" in data and "build_table" in data:
            records["build"].append(data["build_table"][data["build"]][mask])
        else:
            records["build"].append(np.full(int(mask.sum()), "GRCh37", dtype=str))
    records = {k: np.concatenate(v) if v else np.array([]) for k, v in records.items()}
    excluded_status_counts = {}
    non_dominant_counts = {}
    events, source_by_event = classify_point_mutations(
        records,
        retained,
        tsg_rows,
        params,
        oncokb=oncokb,
        row_symbols=row_symbols,
        excluded_counts=excluded_status_counts,
        non_dominant_counts=non_dominant_counts,
    )
    association_events = {POINT_MUTATION: events, STRUCTURAL_VARIANT: set()}
    association_profiled = {POINT_MUTATION: retained}
    pm_events = np.zeros((n_genes, n_codes), dtype=np.int64)
    pm_oncokb_events = np.zeros((n_genes, n_codes), dtype=np.int64)
    pm_hotspot_events = np.zeros((n_genes, n_codes), dtype=np.int64)
    for (row, sample), src in source_by_event.items():
        cohort_idx = sample_cohort[sample]
        pm_events[row, cohort_idx] += 1
        if src == "oncokb":
            pm_oncokb_events[row, cohort_idx] += 1
        else:
            pm_hotspot_events[row, cohort_idx] += 1
    pm = fisher(pm_events, profiled_counts)
    pm["q_value"] = np.where(np.isfinite(pm["p_value"]), bh(pm["p_value"]), np.nan)
    driver_mask = np.ones(n_genes, dtype=bool)

    def pm_gene_reason(i, j):
        return None

    # Copy Number Alteration ------------------------------------------------------------
    amp = np.zeros((n_genes, n_codes), dtype=np.int64)
    deletion, measured = np.zeros_like(amp), np.zeros_like(amp)
    present = np.zeros(n_genes, dtype=bool)
    for j, study in enumerate(studies):
        data, cases = inputs[study]["cna"]["data"], inputs[study]["cna"]["cases"]
        columns = list(data["samples"])
        listed = cases & set(columns)
        # Exactly one GISTIC column per selected patient: the column whose sample ID equals
        # the patient's selected expression sample; if the file repeats that ID, the first
        # such column in file order. Other samples of the patient never count.
        selected, profiled = set(selection[study]), []
        for k, sample_id in enumerate(columns):
            if sample_id in listed and sample_id in selected:
                selected.discard(sample_id)
                profiled.append(k)
        reasons[COPY_NUMBER_ALTERATION][j] = _cohort_reason(
            "Copy Number Alteration", listed, profiled
        )
        cohorts[codes[j]][COPY_NUMBER_ALTERATION] = dict(
            selected=len(selection[study]), n_profiled=len(profiled)
        )
        rows = genes.rows(data["symbol"], data["entrez"])
        present[rows[rows >= 0]] = True
        a, d, m = cna_counts(data["calls"][:, profiled], rows, n_genes)
        amp[:, j], deletion[:, j], measured[:, j] = a, d, m
    cna = {"amp": fisher(amp, measured), "del": fisher(deletion, measured)}
    # Copy-number driver scope: rows on the list and the direction(s) their role allows.
    # Symbols resolving to one row combine (an OG label and a TSG label allow both).
    resolved_cna = {symbol: genes.entrez(symbol) for symbol in sorted(cna_driver.genes)}
    cna_roles = np.zeros((n_genes, 2), dtype=bool)  # oncogene, tumor suppressor
    cna_driver_rows = np.zeros(n_genes, dtype=bool)
    for symbol, entrez in resolved_cna.items():
        if entrez in genes.row:
            row = genes.row[entrez]
            cna_driver_rows[row] = True
            cna_roles[row, 0] |= symbol in cna_driver.oncogenes
            cna_roles[row, 1] |= symbol in cna_driver.tumor_suppressors
    if params.get("cna_direction_by_role", True):
        only_og, only_tsg = cna_roles[:, 0] & ~cna_roles[:, 1], cna_roles[:, 1] & ~cna_roles[:, 0]
        cna_allowed = np.stack([cna_driver_rows & ~only_tsg, cna_driver_rows & ~only_og], axis=1)
    else:
        cna_allowed = np.stack([cna_driver_rows, cna_driver_rows], axis=1)
    # One BH family per gene x direction across the 32 cohorts (as for PM and SV).
    for part in cna.values():
        part["q_value"] = np.where(np.isfinite(part["p_value"]), bh(part["p_value"]), np.nan)

    def cna_gene_reason(i, j):
        if not cna_driver_rows[i]:
            return NOT_CNA_DRIVER
        if not present[i]:
            return "gene not present in GISTIC copy-number data"
        if measured[i, j] == 0:
            return "no measured copy-number calls for this gene in this cohort"
        return None

    # Structural Variant -----------------------------------------------------------------
    sv_counts = np.zeros((n_genes, n_codes), dtype=np.int64)
    sv_profiled = np.zeros(n_codes, dtype=np.int64)
    for j, study in enumerate(studies):
        data, cases = inputs[study]["sv"]["data"], inputs[study]["sv"]["cases"]
        profiled = set(selection[study]) & cases
        sv_profiled[j] = len(profiled)
        reasons[STRUCTURAL_VARIANT][j] = _cohort_reason("structural variant", cases, profiled)
        cohorts[codes[j]][STRUCTURAL_VARIANT] = dict(
            selected=len(selection[study]), n_profiled=len(profiled)
        )
        keep = np.isin(data["sample"], list(profiled))
        pairs = set()
        for site in ("site1", "site2"):
            rows = genes.rows(data[site][keep])
            pairs.update(
                (int(r), s) for r, s in zip(rows, data["sample"][keep], strict=True) if r >= 0
            )
        association_events[STRUCTURAL_VARIANT].update(pairs)
        association_profiled.setdefault(STRUCTURAL_VARIANT, set()).update(profiled)
        for row, _ in pairs:
            sv_counts[row, j] += 1
    sv = fisher(sv_counts, sv_profiled)
    sv["q_value"] = np.where(np.isfinite(sv["p_value"]), bh(sv["p_value"]), np.nan)

    reasons = {c: {j: r for j, r in v.items() if r} for c, v in reasons.items()}
    non_dominant_sorted = dict(
        sorted(non_dominant_counts.items(), key=lambda kv: (-kv[1], str(kv[0])))
    )
    summary = dict(
        cohorts=cohorts,
        point_mutation_mode=("oncokb+hotspot_fallback" if oncokb is not None else "hotspot_only"),
        point_mutation_events=int(pm_events.sum()),
        point_mutation_events_by_source=dict(
            oncokb=int(pm_oncokb_events.sum()),
            hotspot=int(pm_hotspot_events.sum()),
        ),
        point_mutation_oncokb_excluded_counts=excluded_status_counts,
        point_mutation_non_dominant_transcript_rows=sum(non_dominant_sorted.values()),
        point_mutation_non_dominant_transcript_genes=len(non_dominant_sorted),
        point_mutation_non_dominant_transcript_counts_by_gene=non_dominant_sorted,
        point_mutation_tsg_fallback_genes=len(tsg_rows),
        oncokb_api_calls=(oncokb.api_calls if oncokb is not None else 0),
        oncokb_unique_variants_queried=(
            oncokb.unique_variants_queried if oncokb is not None else 0
        ),
        oncokb_cache_hits=(oncokb.cache_hits if oncokb is not None else 0),
        cna_driver_genes=len(cna_driver.genes),
        cna_driver_genes_resolved=int(cna_driver_rows.sum()),
        cna_driver_genes_unresolved=sorted(s for s, e in resolved_cna.items() if e is None),
        cna_driver_directions=dict(
            amp_only=int((cna_allowed[:, 0] & ~cna_allowed[:, 1]).sum()),
            del_only=int((cna_allowed[:, 1] & ~cna_allowed[:, 0]).sum()),
            both=int((cna_allowed[:, 0] & cna_allowed[:, 1]).sum()),
        ),
    )
    result = Alterations(
        codes,
        {POINT_MUTATION: pm, COPY_NUMBER_ALTERATION: cna, STRUCTURAL_VARIANT: sv},
        reasons,
        {
            POINT_MUTATION: pm_gene_reason,
            COPY_NUMBER_ALTERATION: cna_gene_reason,
            STRUCTURAL_VARIANT: lambda i, j: None,
        },
        summary,
        params,
        cna_allowed=cna_allowed,
        pm_sources=(pm_oncokb_events, pm_hotspot_events),
    )

    result.association_inputs = dict(
        inputs=inputs, mapping=mapping, selection=selection, genes=genes,
        events=association_events, profiled=association_profiled,
        driver_mask=driver_mask, cna_allowed=cna_allowed,
    )
    return result
