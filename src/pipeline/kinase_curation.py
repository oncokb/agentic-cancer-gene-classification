"""Fusion-level kinase curation export helpers."""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Iterable, List, Optional

from src.models.schema import AnnotationResult, GeneAnnotation, KinaseFusionCurationRow
from src.pipeline.normalization import split_fusion

KINASE_TERMS = re.compile(
    r"\b(?:protein\s+)?(?:tyrosine|serine/threonine|serine-threonine|lipid|"
    r"receptor\s+tyrosine)?\s*kinase\b",
    re.IGNORECASE,
)
NON_FUNCTIONAL_KINASE_TERMS = re.compile(r"\b(?:pseudo[-\s]?kinase|kinase[-\s]?like)\b", re.IGNORECASE)

KINASE_CURATION_CSV_HEADERS = [
    "Fusion detected",
    "Fusion meta data (gene transcripts/ genomic/transcriptiomic breakpoints exons incl etc)",
    "Kinase included in fusion",
    "Biologic characterization specs",
    "publication link",
]


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
