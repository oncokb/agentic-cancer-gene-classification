"""Spreadsheet export helpers for full annotation results."""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Iterable, Optional

from src.models.schema import AnnotationResult, GeneAnnotation


ANNOTATION_RESULTS_CSV_HEADERS = [
    "gene",
    "fusions",
    "in_oncokb",
    "cancer_associated",
    "cancer_association_rationale",
    "cancer_associated_gene_tier",
    "og_or_tsg",
    "cancer_type_prevalence",
    "gene_class",
    "signaling_pathways",
    "gene_summary",
    "supporting_citation_pmids",
    "supporting_citation_publication_links",
    "date_annotated",
    "retrieval_count",
    "retrieved_pmids",
    "insufficient_evidence",
    "confidence",
    "error",
]


def _format_optional_bool(value: Optional[bool]) -> str:
    if value is None:
        return ""
    return "TRUE" if value else "FALSE"


def _format_bool(value: bool) -> str:
    return "TRUE" if value else "FALSE"


def _format_optional_text(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _unique(values: Iterable[str]) -> list[str]:
    seen = set()
    unique_values = []
    for value in values:
        if value not in seen:
            seen.add(value)
            unique_values.append(value)
    return unique_values


def _publication_links(pmids: Iterable[str]) -> list[str]:
    return [f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" for pmid in pmids]


def build_annotation_results_csv_rows(
    result: AnnotationResult,
) -> list[dict[str, str]]:
    """Build Google Sheets-friendly rows from full gene annotations."""
    rows = []
    for annotation in result.annotations:
        citations = _unique(annotation.citations)
        rows.append(annotation_to_csv_row(annotation, citations=citations))
    return rows


def annotation_to_csv_row(
    annotation: GeneAnnotation,
    citations: Optional[list[str]] = None,
) -> dict[str, str]:
    """Flatten one structured gene annotation into a CSV row."""
    pmids = citations if citations is not None else _unique(annotation.citations)
    return {
        "gene": annotation.gene,
        "fusions": "; ".join(_unique(annotation.fusions)),
        "in_oncokb": _format_optional_bool(annotation.in_oncokb),
        "cancer_associated": _format_optional_bool(annotation.cancer_associated),
        "cancer_association_rationale": _format_optional_text(
            annotation.cancer_association_rationale
        ),
        "cancer_associated_gene_tier": _format_optional_text(
            annotation.cancer_associated_gene_tier
        ),
        "og_or_tsg": _format_optional_text(annotation.og_or_tsg),
        "cancer_type_prevalence": _format_optional_text(
            annotation.cancer_type_prevalence
        ),
        "gene_class": _format_optional_text(annotation.gene_class),
        "signaling_pathways": _format_optional_text(annotation.signaling_pathways),
        "gene_summary": _format_optional_text(annotation.gene_summary),
        "supporting_citation_pmids": "; ".join(pmids),
        "supporting_citation_publication_links": "; ".join(_publication_links(pmids)),
        "date_annotated": annotation.date_annotated,
        "retrieval_count": str(annotation.retrieval_count),
        "retrieved_pmids": "; ".join(_unique(annotation.retrieved_pmids)),
        "insufficient_evidence": _format_bool(annotation.insufficient_evidence),
        "confidence": str(annotation.confidence),
        "error": _format_optional_text(annotation.error),
    }


def write_annotation_results_csv(result: AnnotationResult, path: str | Path) -> None:
    """Write full annotation results as one CSV row per gene."""
    csv_text = annotation_results_csv_text(result)
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write(csv_text)


def annotation_results_csv_text(result: AnnotationResult) -> str:
    """Return full annotation results as CSV text."""
    rows = build_annotation_results_csv_rows(result)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=ANNOTATION_RESULTS_CSV_HEADERS)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()
