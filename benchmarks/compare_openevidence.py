"""Create reproducible per-gene metrics from two completed live arms.

Usage: uv run python -m benchmarks.compare_openevidence RESULTS_DIRECTORY
Reference recall uses existing holdout PMIDs only; absent/empty reference sets
produce null recall, never a fabricated zero or perfect score.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from benchmarks.run_openevidence_benchmark import write_json

CLASSIFICATION_FIELDS = ("cancer_associated", "in_oncokb", "insufficient_evidence")
TEXT_FIELDS = ("gene_summary", "cancer_association_rationale", "gene_class", "signaling_pathways")


def tokens(calls: list[dict]) -> dict:
    result = {key: sum(call.get(key, 0) for call in calls) for key in (
        "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
    )}
    result["total_input_tokens"] = sum(result[key] for key in (
        "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
    ))
    result["calls"] = len(calls)
    return result


def compare(disabled: dict, enabled: dict, gold: dict) -> dict:
    if disabled["status"] != "complete" or enabled["status"] != "complete":
        raise ValueError("Both arms must be complete")
    if disabled["genes"] != enabled["genes"]:
        raise ValueError("Arms must request the same gene list in the same order")
    if set(disabled["per_gene"]) != set(enabled["per_gene"]):
        raise ValueError("Resolved gene sets differ")
    rows = []
    for gene in sorted(disabled["per_gene"]):
        pair = [arm["per_gene"][gene] for arm in (disabled, enabled)]
        annotations = [row["annotation"] for row in pair]
        citation_sets = [set(a["citations"]) for a in annotations]
        reference = set(gold.get(gene, {}).get("citations", []))
        row = {"gene": gene, "reference_pmids": sorted(reference)}
        for label, raw, annotation, citations in zip(
            ("disabled", "enabled"), pair, annotations, citation_sets,
        ):
            row[label] = {
                "retrieval_tier": raw["retrieval_tier"],
                "retrieved_count": annotation["retrieval_count"],
                "citations": sorted(citations), "citation_count": len(citations),
                "reference_hits": sorted(citations & reference),
                "reference_recall": len(citations & reference) / len(reference) if reference else None,
                "synthesis_tokens": tokens([c for c in raw["llm_calls"]
                                            if c["purpose"].startswith("synthesis")]),
                "all_llm_tokens": tokens(raw["llm_calls"]),
                "latency_seconds": annotation["timings_ms"]["total"] / 1000,
                "openevidence_seconds": raw.get("openevidence_seconds", 0),
                "openevidence_succeeded": raw.get("openevidence_succeeded", False),
                "supplementary_citation_count": len(
                    (raw.get("openevidence_analysis") or {}).get("citations", [])
                ),
                "classification": {k: annotation.get(k) for k in CLASSIFICATION_FIELDS},
                "error": annotation["error"],
            }
        row["classification_changes"] = {
            key: [a.get(key) for a in annotations] for key in CLASSIFICATION_FIELDS
            if annotations[0].get(key) != annotations[1].get(key)
        }
        row["text_changes"] = {
            key: [a.get(key) for a in annotations] for key in TEXT_FIELDS
            if annotations[0].get(key) != annotations[1].get(key)
        }
        row["citations_added"] = sorted(citation_sets[1] - citation_sets[0])
        row["citations_removed"] = sorted(citation_sets[0] - citation_sets[1])
        row["identical_retrieval_pool"] = (
            {r["pmid"] for r in pair[0]["records"]} == {r["pmid"] for r in pair[1]["records"]}
        )
        row["identical_selected_pmids"] = set(pair[0]["selected_pmids"]) == set(
            pair[1]["selected_pmids"]
        )
        rows.append(row)
    return {"per_gene": rows, "wall_seconds": {
        "disabled": disabled["wall_seconds"], "enabled": enabled["wall_seconds"],
    }}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    holdout = Path(__file__).parent / "data" / "holdout.jsonl"
    gold = {r["gene"]: r for r in map(json.loads, holdout.read_text().splitlines())}
    arms = [json.loads((args.directory / f"{arm}.json").read_text())
            for arm in ("disabled", "enabled")]
    write_json(args.directory / "comparison.json", compare(*arms, gold))


if __name__ == "__main__":
    main()
