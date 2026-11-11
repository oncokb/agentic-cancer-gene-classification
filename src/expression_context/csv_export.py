"""CSV export for expression-context curation ("golden set" sheets).

Uses the same ``GeneResult`` / ``PairResult`` construction path as
``GET /v1/expression-context/genes/{gene}`` so ``surfaced`` and
``exclusion_reason`` cannot drift from the JSON API or the history list.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import urllib.parse
from datetime import timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from src.expression_context.models import (
    CancerTypeStats,
    GeneStatsView,
    PairStatsView,
    PrecheckState,
)

UTF8_BOM = "\ufeff"
_DANGEROUS_CSV_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
_ENRICHMENT_CODES = {
    "lineage": "lineage_enriched",
    "subgroup": "subgroup_enriched",
}

CSV_COLUMNS: Tuple[str, ...] = (
    "gene_symbol",
    "entrez_gene_id",
    "queried_as",
    "oncotree_code",
    "cancer_type_name",
    "surfaced",
    "exclusion_reason",
    "stats_pass",
    "enrichment_label",
    "broadly_expressed",
    "n_tumor",
    "n_rest",
    "median_tumor",
    "median_rest",
    "log2_fold_change",
    "cliffs_delta",
    "auc",
    "q_value",
    "subgroup_fraction_high",
    "subgroup_fold_over_rest",
    "subgroup_q_value",
    "pubmed_precheck",
    "pubmed_precheck_count",
    "literature_verdict",
    "literature_rule",
    "tumor_specific_pmids",
    "rationale",
    "cbioportal_cancer_type_url",
    "cbioportal_mrna_vs_dx_url",
    "cbioportal_all_32_url",
    "release_id",
    "method_version",
    "literature_checked_at",
)

GOLDEN_SET_COLUMNS: Tuple[str, ...] = (
    "Genes",
    "Relevant expression-based cancer type(s)",
    "Clinically associated cancer type(s)",
    "Relevant alteration type(s)",
    "OG or TSG",
    "Biologic Tags",
    "Drug Target Tags",
    "Other Clinically relevant tags",
    "Category",
    "Obscured no-O category information",
    "Associated drugs",
    "Drug status",
    "Evidence type",
    "Evidence-associated PMIDs",
)


def cbioportal_expression_url(
    symbol: str, study_ids: Sequence[str], horizontal: str
) -> str:
    """Public cBioPortal expression plot URL (matches ``expressionURL`` in JS).

    ``CANCER_STUDY`` with a single study silently opens the Mutation Count plot
    in cBioPortal, so single-study links fall back to ``CANCER_TYPE_DETAILED``.
    """
    if not symbol or not study_ids:
        return ""
    selected = (
        "CANCER_TYPE_DETAILED"
        if horizontal == "CANCER_STUDY" and len(study_ids) == 1
        else horizontal
    )
    query = urllib.parse.urlencode(
        {
            "cancer_study_list": ",".join(study_ids),
            "gene_list": symbol,
            "case_set_id": "all",
            "plots_horz_selection": json.dumps(
                {"dataType": "clinical_attribute", "selectedDataSourceOption": selected},
                separators=(",", ":"),
            ),
            "plots_vert_selection": json.dumps(
                {
                    "dataType": "MRNA_EXPRESSION",
                    "selectedDataSourceOption": "rna_seq_v2_mrna",
                    "logScale": "true",
                },
                separators=(",", ":"),
            ),
        }
    )
    return f"https://www.cbioportal.org/results/plots?{query}"


def precheck_state(stats: CancerTypeStats, expired: bool) -> PrecheckState:
    if expired and stats.literature_precheck_pass is not None:
        return "expired"
    if stats.literature_precheck_pass is True:
        return "pass"
    if stats.literature_precheck_pass is False:
        return "fail"
    return "not_run"


def guard_csv_text(value: Optional[str]) -> str:
    """Prefix formula-triggering leading characters on text cells."""
    if value is None or value == "":
        return ""
    text = str(value)
    if text.startswith(_DANGEROUS_CSV_PREFIXES):
        return f"'{text}"
    return text


def format_int(value: Optional[int]) -> str:
    if value is None or isinstance(value, bool):
        return ""
    return str(int(value))


def format_float(value: Optional[float]) -> str:
    """Plain decimal rounded to 4 significant digits, no thousands separators."""
    if value is None or isinstance(value, bool):
        return ""
    num = float(value)
    if not math.isfinite(num):
        return ""
    if num == 0.0:
        return "0"
    dec = Decimal(f"{num:.4g}").normalize()
    text = format(dec, "f")
    return "0" if text == "-0" else text


def format_q(value: Optional[float]) -> str:
    """4 significant digits, keeping scientific notation for small q-values."""
    if value is None or isinstance(value, bool):
        return ""
    num = float(value)
    if not math.isfinite(num):
        return ""
    if num == 0.0:
        return "0"
    return f"{num:.4g}"


def _format_yes_no(value: Optional[bool]) -> str:
    if value is None:
        return ""
    return "yes" if value else "no"


def _enrichment_code(stats: CancerTypeStats) -> str:
    if not stats.passes_stats or stats.pass_mode is None:
        return ""
    return _ENRICHMENT_CODES.get(stats.pass_mode, stats.pass_mode)


def csv_filename(gene_or_all: str, release_id: str) -> str:
    safe_gene = _SAFE_FILENAME.sub("_", (gene_or_all or "all").strip()) or "all"
    safe_release = _SAFE_FILENAME.sub("_", (release_id or "unknown").strip()) or "unknown"
    return f"expression-context_{safe_gene}_{safe_release}.csv"


def format_csv_rows(
    rows: Iterable[Sequence[str]], *, include_header: bool = False, include_bom: bool = False
) -> str:
    buf = io.StringIO()
    if include_bom:
        buf.write(UTF8_BOM)
    writer = csv.writer(buf, lineterminator="\r\n")
    if include_header:
        writer.writerow(CSV_COLUMNS)
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


def golden_set_filename(release_id: str) -> str:
    safe_release = _SAFE_FILENAME.sub("_", (release_id or "unknown").strip()) or "unknown"
    return f"expression-context_golden-set_{safe_release}.csv"


class UnmappedCancerTypeError(RuntimeError):
    """Raised when the active bundle contains a cohort code missing from sheet_cancer_names.json."""
    pass


def check_sheet_cancer_names(bundle: Any, name_map: Mapping[str, str]) -> None:
    """Verify that every cohort code in the bundle manifest is present in name_map."""
    manifest = getattr(bundle, "manifest", bundle)
    cancer_types = getattr(manifest, "cancer_types", None)
    if cancer_types is None and isinstance(manifest, dict):
        cancer_types = manifest.get("cancer_types", [])
    if not cancer_types:
        return
    unmapped = []
    for ct in cancer_types:
        code = getattr(ct, "oncotree_code", None)
        if code is None and isinstance(ct, dict):
            code = ct.get("oncotree_code")
        if code and str(code) not in name_map:
            unmapped.append(str(code))
    if unmapped:
        raise UnmappedCancerTypeError(
            f"Missing golden-set sheet cancer name mapping for cohort code(s): {', '.join(sorted(set(unmapped)))}"
        )


def load_sheet_cancer_names(path: Optional[Union[str, Path]] = None) -> Dict[str, str]:
    target = (
        Path(path)
        if path is not None
        else Path(__file__).resolve().parent / "sheet_cancer_names.json"
    )
    with open(target, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {k: str(v) for k, v in data.items() if k != "version"}


def format_golden_set_rows(
    rows: Iterable[Sequence[str]], *, include_header: bool = False, include_bom: bool = False
) -> str:
    buf = io.StringIO()
    if include_bom:
        buf.write(UTF8_BOM)
    writer = csv.writer(buf, lineterminator="\r\n")
    if include_header:
        writer.writerow(GOLDEN_SET_COLUMNS)
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


def golden_set_row_from_view(
    view: GeneStatsView,
    *,
    name_map: Optional[Mapping[str, str]] = None,
) -> List[str]:
    if name_map is None:
        name_map = load_sheet_cancer_names()

    surfaced = [p for p in view.pairs if p.status == "surfaced"]
    if surfaced:
        surfaced_codes = sorted({p.oncotree_code for p in surfaced})
        names: List[str] = []
        for code in surfaced_codes:
            if code not in name_map:
                raise KeyError(f"No sheet cancer name for surfaced cohort code {code!r}")
            name = name_map[code]
            if name not in names:
                names.append(name)
        cancer_types_str = "; ".join(names)
        alteration_str = "Expression"

        pmid_set = set()
        for p in surfaced:
            if p.literature and p.literature.pmids:
                for pmid in p.literature.pmids:
                    s = str(pmid).strip()
                    if s:
                        pmid_set.add(s)
        sorted_pmids = sorted(pmid_set, key=lambda x: (int(x) if x.isdigit() else float("inf"), x))
        pmids_str = "; ".join(sorted_pmids)
    else:
        cancer_types_str = ""
        alteration_str = ""
        pmids_str = ""

    return [
        guard_csv_text(view.hugo_symbol),
        guard_csv_text(cancer_types_str),
        "",
        guard_csv_text(alteration_str),
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        guard_csv_text(pmids_str),
    ]


def pair_view_to_csv_row(
    view: GeneStatsView,
    pair: PairStatsView,
    *,
    all_study_ids: Sequence[str] = (),
) -> List[str]:
    stats = pair.stats
    code = pair.oncotree_code
    cancer_name = pair.cancer_type_name
    study_id = pair.study_id
    surfaced = pair.status == "surfaced"
    if surfaced:
        reason = ""
    elif pair.status == "literature_pending":
        reason = "pending"
    else:
        reason = pair.exclusion_reason or ""

    verdict = pair.literature
    verdict_str = verdict.verdict if verdict is not None and verdict.verdict else None
    rule_str = (
        verdict.literature_gate
        if verdict is not None and verdict.literature_gate
        else view.literature_gate
    )
    pmids_str = ";".join(verdict.pmids) if verdict is not None and verdict.pmids else ""
    rationale_str = verdict.rationale if verdict is not None and verdict.rationale else ""
    checked_at = (
        verdict.evaluated_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        if verdict is not None and verdict.evaluated_at is not None
        else ""
    )
    single_url = (
        cbioportal_expression_url(view.hugo_symbol, [study_id], "CANCER_STUDY")
        if study_id
        else ""
    )
    mrna_vs_dx_url = (
        cbioportal_expression_url(view.hugo_symbol, [study_id], "CANCER_TYPE_DETAILED")
        if study_id
        else ""
    )
    all_32_url = (
        cbioportal_expression_url(view.hugo_symbol, list(all_study_ids), "CANCER_STUDY")
        if all_study_ids
        else ""
    )
    return [
        guard_csv_text(view.hugo_symbol),
        format_int(view.entrez_gene_id),
        guard_csv_text(view.query),
        guard_csv_text(code),
        guard_csv_text(cancer_name),
        guard_csv_text(_format_yes_no(surfaced)),
        guard_csv_text(reason),
        guard_csv_text(_format_yes_no(stats.passes_stats)),
        guard_csv_text(_enrichment_code(stats)),
        guard_csv_text(_format_yes_no(view.broadly_expressed)),
        format_int(stats.n),
        format_int(stats.n_rest),
        format_float(stats.median_log2),
        format_float(stats.rest_median_log2),
        format_float(stats.log2fc),
        format_float(stats.cliffs_delta),
        format_float(stats.auc),
        format_q(stats.q_value),
        format_float(stats.subgroup_high_fraction),
        format_float(stats.subgroup_high_fraction_ratio),
        format_q(stats.subgroup_q),
        guard_csv_text(pair.precheck),
        format_int(stats.pubmed_count),
        guard_csv_text(verdict_str),
        guard_csv_text(rule_str),
        guard_csv_text(pmids_str),
        guard_csv_text(rationale_str),
        guard_csv_text(single_url),
        guard_csv_text(mrna_vs_dx_url),
        guard_csv_text(all_32_url),
        guard_csv_text(view.bundle.release_id),
        guard_csv_text(view.bundle.method_version),
        guard_csv_text(checked_at),
    ]


def gene_view_csv_rows(
    view: GeneStatsView,
    *,
    include_excluded: bool = False,
    all_study_ids: Sequence[str] = (),
) -> List[List[str]]:
    study_ids = list(all_study_ids)
    if not study_ids:
        seen = set()
        for p in view.pairs:
            if p.study_id and p.study_id not in seen:
                seen.add(p.study_id)
                study_ids.append(p.study_id)
    surfaced = sorted(
        (p for p in view.pairs if p.status == "surfaced"),
        key=lambda p: (
            p.stats.q_value is None,
            p.stats.q_value if p.stats.q_value is not None else 0.0,
            p.oncotree_code,
        ),
    )
    pairs: List[PairStatsView] = list(surfaced)
    if include_excluded:
        excluded = sorted(
            (p for p in view.pairs if p.status != "surfaced"),
            key=lambda p: p.oncotree_code,
        )
        pairs.extend(excluded)
    return [pair_view_to_csv_row(view, p, all_study_ids=study_ids) for p in pairs]
