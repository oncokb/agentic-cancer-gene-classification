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
from dataclasses import dataclass, field
from datetime import timezone
from decimal import Decimal
from typing import Iterable, List, Mapping, Optional, Sequence, Tuple

from src.expression_context.models import (
    CancerTypeStats,
    GeneResult,
    PairResult,
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


@dataclass(frozen=True)
class GeneCsvContext:
    """Bundle/gene metadata joined onto a ``GeneResult`` when formatting CSV rows."""

    queried_as: str
    entrez_gene_id: Optional[int] = None
    method_version: Optional[str] = None
    broadly_expressed: Optional[bool] = None
    all_study_ids: Tuple[str, ...] = ()
    study_id_by_code: Mapping[str, str] = field(default_factory=dict)
    cancer_name_by_code: Mapping[str, str] = field(default_factory=dict)
    expired_by_code: Mapping[str, bool] = field(default_factory=dict)


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


def pair_to_csv_row(
    result: GeneResult,
    pair: PairResult,
    context: GeneCsvContext,
) -> List[str]:
    stats = pair.stats
    code = pair.oncotree_code
    cancer_name = pair.cancer_type_name or context.cancer_name_by_code.get(code)
    study_id = pair.study_id or context.study_id_by_code.get(code)
    expired = bool(context.expired_by_code.get(code, False))
    precheck = precheck_state(stats, expired)
    broadly = (
        result.broadly_expressed
        if result.broadly_expressed is not None
        else context.broadly_expressed
    )
    verdict = pair.literature
    verdict_str = verdict.verdict if verdict is not None and verdict.verdict else None
    rule_str = (
        verdict.literature_gate
        if verdict is not None and verdict.literature_gate
        else result.literature_gate
    )
    pmids_str = ";".join(verdict.pmids) if verdict is not None and verdict.pmids else ""
    rationale_str = verdict.rationale if verdict is not None and verdict.rationale else ""
    checked_at = (
        verdict.evaluated_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        if verdict is not None and verdict.evaluated_at is not None
        else ""
    )
    single_url = (
        cbioportal_expression_url(result.hugo_symbol, [study_id], "CANCER_STUDY")
        if study_id
        else ""
    )
    mrna_vs_dx_url = (
        cbioportal_expression_url(result.hugo_symbol, [study_id], "CANCER_TYPE_DETAILED")
        if study_id
        else ""
    )
    all_32_url = (
        cbioportal_expression_url(
            result.hugo_symbol, list(context.all_study_ids), "CANCER_STUDY"
        )
        if context.all_study_ids
        else ""
    )
    return [
        guard_csv_text(result.hugo_symbol),
        format_int(context.entrez_gene_id),
        guard_csv_text(context.queried_as),
        guard_csv_text(code),
        guard_csv_text(cancer_name),
        guard_csv_text(_format_yes_no(pair.surfaced)),
        guard_csv_text(pair.exclusion_reason),
        guard_csv_text(_format_yes_no(stats.passes_stats)),
        guard_csv_text(_enrichment_code(stats)),
        guard_csv_text(_format_yes_no(broadly)),
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
        guard_csv_text(precheck),
        format_int(stats.pubmed_count),
        guard_csv_text(verdict_str),
        guard_csv_text(rule_str),
        guard_csv_text(pmids_str),
        guard_csv_text(rationale_str),
        guard_csv_text(single_url),
        guard_csv_text(mrna_vs_dx_url),
        guard_csv_text(all_32_url),
        guard_csv_text(result.release_id),
        guard_csv_text(context.method_version),
        guard_csv_text(checked_at),
    ]


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


def gene_result_csv_rows(
    result: GeneResult, context: GeneCsvContext
) -> List[List[str]]:
    pairs: List[PairResult] = list(result.surfaced)
    if result.excluded is not None:
        pairs.extend(result.excluded)
    return [pair_to_csv_row(result, pair, context) for pair in pairs]
