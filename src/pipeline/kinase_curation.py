"""Fusion-level kinase curation export helpers."""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from src.models.schema import AnnotationResult, GeneAnnotation, KinaseFusionCurationRow
from src.pipeline.normalization import split_fusion

KINASE_TERMS = re.compile(
    r"\b(?:protein\s+)?(?:tyrosine|serine/threonine|serine-threonine|lipid|"
    r"receptor\s+tyrosine)?\s*kinase\b",
    re.IGNORECASE,
)
NON_FUNCTIONAL_KINASE_TERMS = re.compile(
    r"\b(?:pseudo[-\s]?kinase|kinase[-\s]?like)\b",
    re.IGNORECASE,
)

KINASE_CURATION_CSV_HEADERS = [
    "Fusion detected",
    "Fusion meta data (gene transcripts/ genomic/transcriptiomic breakpoints exons incl etc)",
    "Kinase included in fusion",
    "Biologic characterization specs",
    "publication link",
]

KINASE_COMPARISON_CSV_HEADERS = [
    "comparison_status",
    "fusion_detected",
    "kinase_included_in_fusion",
    "citation_precision",
    "citation_recall",
    "citation_f1",
    "citation_tp",
    "citation_fp_pipeline_only",
    "citation_fn_truth_only",
    "pipeline_publication_link",
    "truth_publication_link",
    "pipeline_biologic_characterization_specs",
    "truth_biologic_characterization_specs",
]

FUSION_COLUMN_ALIASES = [
    "Fusion detected",
    "Fusion",
    "Fusion name",
    "Fusion event",
]
METADATA_COLUMN_ALIASES = [
    "Fusion meta data (gene transcripts/ genomic/transcriptiomic breakpoints exons incl etc)",
    "Fusion metadata",
    "Fusion meta data",
    "Metadata",
]
KINASE_COLUMN_ALIASES = [
    "Kinase included in fusion",
    "Kinase",
    "Kinase gene",
    "Kinase partner",
]
BIOLOGIC_COLUMN_ALIASES = [
    "Biologic characterization specs",
    "Biological characterization specs",
    "Biologic characterization",
    "Functional characterization",
]
PUBLICATION_COLUMN_ALIASES = [
    "publication link",
    "Publication link",
    "Publication",
    "PMID",
    "PMIDs",
    "Citations",
]

PMID_PATTERN = re.compile(r"\b(?:PMID\s*[:#]?\s*)?(\d{5,9})\b", re.IGNORECASE)


def _contains_functional_kinase_term(value: Optional[str]) -> bool:
    if not value:
        return False
    if NON_FUNCTIONAL_KINASE_TERMS.search(value):
        return False
    return bool(KINASE_TERMS.search(value))


def _is_functional_kinase(annotation: GeneAnnotation) -> bool:
    """Conservatively detect kinase annotations from structured functional fields."""
    return _contains_functional_kinase_term(annotation.gene_class)


def _format_fusion_metadata(fusion: str) -> str:
    partner_a, partner_b = split_fusion(fusion)
    partners = partner_a if partner_b is None else f"{partner_a}, {partner_b}"
    return (
        f"Fusion partners: {partners}; gene transcripts/genomic breakpoints/"
        "transcriptomic breakpoints/exons: not provided in input"
    )


def _publication_links(citations: Iterable[str]) -> str:
    return "; ".join(
        f"PMID {pmid}: https://pubmed.ncbi.nlm.nih.gov/{pmid}/" for pmid in citations
    )


def _extract_pmids(value: str) -> List[str]:
    seen = set()
    pmids = []
    for match in PMID_PATTERN.finditer(value or ""):
        pmid = match.group(1)
        if pmid not in seen:
            seen.add(pmid)
            pmids.append(pmid)
    return pmids


def _safe_div(num: float, den: float) -> float:
    return num / den if den else 0.0


def _citation_scores(predicted: Sequence[str], truth: Sequence[str]) -> tuple[float, float, float]:
    pred_set = set(predicted)
    truth_set = set(truth)
    if not pred_set and not truth_set:
        return 1.0, 1.0, 1.0
    if not pred_set:
        return 0.0, 0.0, 0.0
    if not truth_set:
        return 0.0, 1.0, 0.0

    true_positive = len(pred_set & truth_set)
    precision = _safe_div(true_positive, len(pred_set))
    recall = _safe_div(true_positive, len(truth_set))
    f1 = _safe_div(2 * precision * recall, precision + recall)
    return precision, recall, f1


def _biologic_specs(annotation: GeneAnnotation) -> str:
    fields = [
        ("gene_class", annotation.gene_class),
        ("tier", annotation.cancer_associated_gene_tier),
        ("OG/TSG", annotation.og_or_tsg),
        ("pathways", annotation.signaling_pathways),
        ("rationale", annotation.cancer_association_rationale),
        ("summary", annotation.gene_summary),
    ]
    specs = [f"{label}: {value}" for label, value in fields if value]
    if annotation.insufficient_evidence:
        specs.append("insufficient evidence in retrieved literature")
    return "; ".join(specs) if specs else "not characterized in retrieved literature"


def _find_column(row: Dict[str, str], aliases: Sequence[str]) -> str:
    lower_to_key = {key.strip().lower(): key for key in row}
    for alias in aliases:
        key = lower_to_key.get(alias.lower())
        if key:
            return row.get(key, "").strip()
    return ""


def _normalize_fusion_key(fusion: str) -> str:
    partner_a, partner_b = split_fusion(fusion)
    if partner_b is None:
        return partner_a.upper()
    return f"{partner_a.upper()}::{partner_b.upper()}"


def _split_kinase_genes(value: str) -> List[str]:
    genes = []
    seen = set()
    for gene in re.split(r"[,;/|]", value or ""):
        normalized = gene.strip().upper()
        if normalized and normalized not in seen:
            seen.add(normalized)
            genes.append(normalized)
    return genes


def _row_keys(row: KinaseFusionCurationRow) -> List[tuple[str, str]]:
    return [
        (_normalize_fusion_key(row.fusion_detected), kinase)
        for kinase in _split_kinase_genes(row.kinase_included_in_fusion)
    ]


def _index_rows(
    rows: Iterable[KinaseFusionCurationRow],
) -> Dict[tuple[str, str], KinaseFusionCurationRow]:
    indexed = {}
    for row in rows:
        for key in _row_keys(row):
            indexed.setdefault(key, row)
    return indexed


def build_kinase_fusion_curation_rows(
    result: AnnotationResult,
) -> List[KinaseFusionCurationRow]:
    """Build one spreadsheet row per input fusion with a functional kinase partner."""
    annotations_by_fusion: dict[str, list[GeneAnnotation]] = {}
    for annotation in result.annotations:
        for fusion in annotation.fusions:
            annotations_by_fusion.setdefault(fusion, []).append(annotation)

    rows: List[KinaseFusionCurationRow] = []
    for fusion, fusion_annotations in annotations_by_fusion.items():
        kinase_annotations = [
            annotation
            for annotation in fusion_annotations
            if _is_functional_kinase(annotation)
        ]
        if not kinase_annotations:
            continue

        rows.append(
            KinaseFusionCurationRow(
                fusion_detected=fusion,
                fusion_metadata=_format_fusion_metadata(fusion),
                kinase_included_in_fusion=", ".join(
                    annotation.gene for annotation in kinase_annotations
                ),
                biologic_characterization_specs=" | ".join(
                    f"{annotation.gene}: {_biologic_specs(annotation)}"
                    for annotation in kinase_annotations
                ),
                publication_link="; ".join(
                    link
                    for annotation in kinase_annotations
                    if (link := _publication_links(annotation.citations))
                ),
            )
        )

    return rows


def write_kinase_fusion_curation_csv(
    rows: Iterable[KinaseFusionCurationRow],
    path: str | Path,
) -> None:
    """Write kinase curation rows using reviewer-facing column names."""
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=KINASE_CURATION_CSV_HEADERS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "Fusion detected": row.fusion_detected,
                    (
                        "Fusion meta data (gene transcripts/ genomic/transcriptiomic "
                        "breakpoints exons incl etc)"
                    ): row.fusion_metadata,
                    "Kinase included in fusion": row.kinase_included_in_fusion,
                    "Biologic characterization specs": row.biologic_characterization_specs,
                    "publication link": row.publication_link,
                }
            )


def read_kinase_fusion_curation_csv(path: str | Path) -> List[KinaseFusionCurationRow]:
    """Read pipeline or Google Sheets-exported kinase curation CSV rows."""
    rows: List[KinaseFusionCurationRow] = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for source_row in reader:
            fusion = _find_column(source_row, FUSION_COLUMN_ALIASES)
            kinase = _find_column(source_row, KINASE_COLUMN_ALIASES)
            if not fusion or not kinase:
                continue
            rows.append(
                KinaseFusionCurationRow(
                    fusion_detected=fusion,
                    fusion_metadata=_find_column(source_row, METADATA_COLUMN_ALIASES),
                    kinase_included_in_fusion=kinase,
                    biologic_characterization_specs=_find_column(
                        source_row,
                        BIOLOGIC_COLUMN_ALIASES,
                    ),
                    publication_link=_find_column(source_row, PUBLICATION_COLUMN_ALIASES),
                )
            )
    return rows


def compare_kinase_curation_rows(
    pipeline_rows: Iterable[KinaseFusionCurationRow],
    truth_rows: Iterable[KinaseFusionCurationRow],
) -> Dict:
    """Compare generated kinase curation rows against a read-only source-of-truth CSV."""
    pipeline_index = _index_rows(pipeline_rows)
    truth_index = _index_rows(truth_rows)
    pipeline_keys = set(pipeline_index)
    truth_keys = set(truth_index)
    matched_keys = pipeline_keys & truth_keys
    pipeline_only_keys = pipeline_keys - truth_keys
    truth_only_keys = truth_keys - pipeline_keys

    per_row = []
    for fusion_key, kinase in sorted(pipeline_keys | truth_keys):
        key = (fusion_key, kinase)
        pipeline_row = pipeline_index.get(key)
        truth_row = truth_index.get(key)
        pipeline_pmids = _extract_pmids(pipeline_row.publication_link) if pipeline_row else []
        truth_pmids = _extract_pmids(truth_row.publication_link) if truth_row else []
        precision, recall, f1 = _citation_scores(pipeline_pmids, truth_pmids)

        if pipeline_row and truth_row:
            status = "matched"
        elif pipeline_row:
            status = "pipeline_only"
        else:
            status = "truth_only"

        per_row.append(
            {
                "comparison_status": status,
                "fusion_detected": fusion_key,
                "kinase_included_in_fusion": kinase,
                "citation_precision": round(precision, 4),
                "citation_recall": round(recall, 4),
                "citation_f1": round(f1, 4),
                "citation_tp": sorted(set(pipeline_pmids) & set(truth_pmids)),
                "citation_fp_pipeline_only": sorted(
                    set(pipeline_pmids) - set(truth_pmids)
                ),
                "citation_fn_truth_only": sorted(set(truth_pmids) - set(pipeline_pmids)),
                "pipeline_publication_link": (
                    pipeline_row.publication_link if pipeline_row else ""
                ),
                "truth_publication_link": truth_row.publication_link if truth_row else "",
                "pipeline_biologic_characterization_specs": (
                    pipeline_row.biologic_characterization_specs if pipeline_row else ""
                ),
                "truth_biologic_characterization_specs": (
                    truth_row.biologic_characterization_specs if truth_row else ""
                ),
            }
        )

    matched_rows = [row for row in per_row if row["comparison_status"] == "matched"]
    mean_precision = (
        sum(row["citation_precision"] for row in matched_rows) / len(matched_rows)
        if matched_rows
        else 0.0
    )
    mean_recall = (
        sum(row["citation_recall"] for row in matched_rows) / len(matched_rows)
        if matched_rows
        else 0.0
    )
    mean_f1 = (
        sum(row["citation_f1"] for row in matched_rows) / len(matched_rows)
        if matched_rows
        else 0.0
    )
    key_precision = _safe_div(len(matched_keys), len(pipeline_keys))
    key_recall = _safe_div(len(matched_keys), len(truth_keys))
    key_f1 = _safe_div(2 * key_precision * key_recall, key_precision + key_recall)

    return {
        "summary": {
            "pipeline_keys": len(pipeline_keys),
            "truth_keys": len(truth_keys),
            "matched_keys": len(matched_keys),
            "pipeline_only_keys": len(pipeline_only_keys),
            "truth_only_keys": len(truth_only_keys),
            "fusion_kinase_precision": round(key_precision, 4),
            "fusion_kinase_recall": round(key_recall, 4),
            "fusion_kinase_f1": round(key_f1, 4),
            "matched_citation_precision": round(mean_precision, 4),
            "matched_citation_recall": round(mean_recall, 4),
            "matched_citation_f1": round(mean_f1, 4),
        },
        "per_row": per_row,
    }


def write_kinase_curation_comparison_csv(report: Dict, path: str | Path) -> None:
    """Write per-row comparison against the source-of-truth sheet export."""
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=KINASE_COMPARISON_CSV_HEADERS)
        writer.writeheader()
        for row in report["per_row"]:
            writer.writerow(
                {
                    **row,
                    "citation_tp": "; ".join(row["citation_tp"]),
                    "citation_fp_pipeline_only": "; ".join(
                        row["citation_fp_pipeline_only"]
                    ),
                    "citation_fn_truth_only": "; ".join(row["citation_fn_truth_only"]),
                }
            )
