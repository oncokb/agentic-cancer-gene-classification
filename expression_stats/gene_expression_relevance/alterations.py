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
from .stats import bh

# Bump when a parser's output changes; summaries are cached per parser version.
PARSER_VERSION = "1"
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
POSITION = re.compile(r"p\.[A-Z*](\d+)[A-Z*]")
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
    """Nonsynonymous rows of every gene, compactly: enough for burden and any driver rule."""
    parts = []
    reader = pd.read_csv(
        path,
        sep="\t",
        usecols=MUTATION_COLUMNS,
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
    frame = (
        pd.concat(parts, ignore_index=True)
        if parts
        else pd.DataFrame({column: pd.Series(dtype=str) for column in MUTATION_COLUMNS})
    )
    samples, sample_table = _table(frame["Tumor_Sample_Barcode"].str.slice(0, 15))
    symbols, symbol_table = _table(frame["Hugo_Symbol"].str.upper())
    transcripts, transcript_table = _table(frame["Transcript_ID"])
    classes = frame["Variant_Classification"].map(CLASS).to_numpy(dtype=np.int8)
    position = (
        frame["HGVSp_Short"].str.extract(POSITION.pattern, expand=False).fillna("-1").astype(int)
    )
    inframe = np.isin(classes, INFRAME)
    hgvsp, hgvsp_table = _table(frame["HGVSp_Short"].where(inframe, ""))
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


def summary(commit, cache, study, kind, *, delete_downloads=False):
    """(oid, parsed summary) for one pinned study file, downloading only on a cache miss."""
    relative = f"public/{study}/{FILES[kind]}"
    oid, size, inline = fetching.locate(commit, relative)
    target = Path(cache) / "alterations" / f"p{PARSER_VERSION}" / kind / f"{oid}.npz"
    if target.exists():
        try:
            with np.load(target, allow_pickle=False) as stored:
                return oid, {key: stored[key] for key in stored.files}
        except (OSError, ValueError, KeyError):
            target.unlink(missing_ok=True)
    if inline is None:
        path = fetching.fetch_lfs(commit, cache, relative, oid, size, f"{study}/{FILES[kind]}")
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


def case_list(commit, cache, study, kind):
    target = Path(cache) / "case_lists" / commit / study / CASE_LISTS[kind]
    if not target.exists():
        data = fetching.pinned_bytes(commit, f"public/{study}/case_lists/{CASE_LISTS[kind]}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    data = target.read_bytes()
    return hashlib.sha256(data).hexdigest(), parse_case_list(data.decode())


def load_inputs(commit, cache, studies, *, workers=4, delete_downloads=False):
    """Per-study summaries and case lists, plus a fingerprint of every input object."""
    jobs = [(study, kind) for study in studies for kind in FILES]

    def run(job):
        study, kind = job
        oid, data = summary(commit, cache, study, kind, delete_downloads=delete_downloads)
        list_sha, cases = case_list(commit, cache, study, kind)
        return job, oid, list_sha, data, cases

    inputs, provenance = {}, {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for (study, kind), oid, list_sha, data, cases in pool.map(run, jobs):
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


def point_mutation_events(records, retained, driver_rows, tsg_rows, params=ALTERATION_PARAMS):
    """Putative-driver point mutations per (gene row, sample) under the driver-source rule.

    records: concatenated mutation rows with keys row (gene row, -1 unresolved), sample
    (sample id string), cls, transcript, position, hgvsp (strings; "" when absent).
    retained: the profiled, non-hypermutated samples; recurrence counts only these.
    A missense counts when its (gene, transcript, protein position) is mutated in at least
    missense_min_recurrence retained tumors pan-cancer; a truncating call counts only in a
    tumor suppressor; an in-frame indel counts when the exact (gene, transcript, protein
    change) recurs in at least inframe_min_recurrence retained tumors.
    """
    keep = np.isin(records["row"], list(driver_rows)) & np.isin(records["sample"], list(retained))
    frame = pd.DataFrame({key: np.asarray(value)[keep] for key, value in records.items()})
    if frame.empty:
        return set()
    missense = frame[(frame["cls"] == MISSENSE) & (frame["position"] >= 0)]
    recurrent = missense.groupby(["row", "transcript", "position"])["sample"].nunique()
    recurrent = recurrent[recurrent >= params["missense_min_recurrence"]]
    hits = missense.join(
        recurrent.rename("tumors"), on=["row", "transcript", "position"], how="inner"
    )
    inframe = frame[frame["cls"].isin(INFRAME) & (frame["hgvsp"] != "")]
    recurring = inframe.groupby(["row", "transcript", "hgvsp"])["sample"].nunique()
    recurring = recurring[recurring >= params["inframe_min_recurrence"]]
    inframe_hits = inframe.join(
        recurring.rename("tumors"), on=["row", "transcript", "hgvsp"], how="inner"
    )
    truncating = frame[frame["cls"].isin(TRUNCATING) & frame["row"].isin(list(tsg_rows))]
    events = set()
    for part in (hits, inframe_hits, truncating):
        events.update(zip(part["row"].tolist(), part["sample"].tolist(), strict=True))
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


def untestable(reason, n_profiled=None, cna=False):
    entry = dict(status="not_testable", reason=reason, n_profiled=n_profiled, **EMPTY)
    entry["direction"] = None
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
    ):
        self.codes = codes
        self.results = results  # category -> fisher dict (CNA: {"amp": ..., "del": ...})
        self.reasons = reasons  # category -> {cohort index: not_testable reason}
        self.gene_reasons = gene_reasons  # category -> callable(i, j) -> reason or None
        self.summary = summary
        self.params = params
        # (genes, 2) booleans: may amp / del be called enriched for this gene row.
        self.cna_allowed = cna_allowed

    def _cna_directions(self, i):
        if self.cna_allowed is None:
            return DIRECTIONS
        return tuple(d for d, ok in zip(DIRECTIONS, self.cna_allowed[i], strict=True) if ok)

    def _category(self, category, i, j):
        reason = self.reasons[category].get(j) or self.gene_reasons[category](i, j)
        cna = category == COPY_NUMBER_ALTERATION
        result = self.results[category]["amp"] if cna else self.results[category]
        n_profiled = int(result["n_profiled"][i, j])
        if reason:
            return untestable(reason, n_profiled, cna)
        minimum = self.params["min_profiled"]
        if not cna:
            entry = dict(status=None, reason=None, n_profiled=n_profiled, **_stats(result, i, j))
            entry["direction"] = None
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
    driver,
    params=ALTERATION_PARAMS,
    *,
    cna_driver,
):
    """Build Alterations for gene rows `ids` (Entrez) over the cohorts in `mapping` order.

    driver: mutation DriverSource; cna_driver: CnaDriverSource (genes off its list are
    CNA not_testable; on-list genes may be called only in their role's direction(s)).
    """
    codes = [item["oncotree_code"] for item in mapping]
    studies = [item["study_id"] for item in mapping]
    genes = Genes(ids, catalog, aliases)
    n_genes, n_codes = len(ids), len(codes)
    reasons = {category: {} for category in ALTERATION_CATEGORIES}
    cohorts = {}
    # Patient-level counts rely on one selected sample per patient across all cohorts.
    patients = [sample_id[:12] for study in studies for sample_id in selection[study]]
    if len(patients) != len(set(patients)):
        raise ValueError("Sample selection must contain at most one sample per patient")

    # Point Mutation ------------------------------------------------------------------
    resolved_drivers = {symbol: genes.entrez(symbol) for symbol in sorted(driver.genes)}
    driver_rows = {genes.row[e] for e in resolved_drivers.values() if e in genes.row}
    tsg_rows = {
        genes.row[resolved_drivers[s]]
        for s in driver.tumor_suppressors
        if resolved_drivers.get(s) in genes.row
    }
    records = {k: [] for k in ("row", "sample", "cls", "transcript", "position", "hgvsp")}
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
        mask = np.isin(rows, list(driver_rows)) & np.isin(sample_ids, list(profiled))
        records["row"].append(rows[mask])
        records["sample"].append(sample_ids[mask])
        records["cls"].append(data["cls"][mask])
        records["transcript"].append(data["transcript_table"][data["transcript"]][mask])
        records["position"].append(data["position"][mask])
        records["hgvsp"].append(data["hgvsp_table"][data["hgvsp"]][mask])
    records = {k: np.concatenate(v) if v else np.array([]) for k, v in records.items()}
    events = point_mutation_events(records, retained, driver_rows, tsg_rows, params)
    pm_events = np.zeros((n_genes, n_codes), dtype=np.int64)
    for row, sample in events:
        pm_events[row, sample_cohort[sample]] += 1
    pm = fisher(pm_events, profiled_counts)
    pm["q_value"] = np.where(np.isfinite(pm["p_value"]), bh(pm["p_value"]), np.nan)
    driver_mask = np.zeros(n_genes, dtype=bool)
    driver_mask[list(driver_rows)] = True

    def pm_gene_reason(i, j):
        if not driver_mask[i]:
            return f"gene not in mutation driver list ({driver.name})"
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
        for row, _ in pairs:
            sv_counts[row, j] += 1
    sv = fisher(sv_counts, sv_profiled)
    sv["q_value"] = np.where(np.isfinite(sv["p_value"]), bh(sv["p_value"]), np.nan)

    reasons = {c: {j: r for j, r in v.items() if r} for c, v in reasons.items()}
    summary = dict(
        cohorts=cohorts,
        mutation_driver_genes=len(driver.genes),
        mutation_driver_genes_resolved=len(driver_rows),
        mutation_driver_genes_unresolved=sorted(
            s for s, e in resolved_drivers.items() if e is None
        ),
        point_mutation_events=int(pm_events.sum()),
        cna_driver_genes=len(cna_driver.genes),
        cna_driver_genes_resolved=int(cna_driver_rows.sum()),
        cna_driver_genes_unresolved=sorted(s for s, e in resolved_cna.items() if e is None),
        cna_driver_directions=dict(
            amp_only=int((cna_allowed[:, 0] & ~cna_allowed[:, 1]).sum()),
            del_only=int((cna_allowed[:, 1] & ~cna_allowed[:, 0]).sum()),
            both=int((cna_allowed[:, 0] & cna_allowed[:, 1]).sum()),
        ),
    )
    return Alterations(
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
    )
