"""DMT index is a reviewed annotation independent of surfacing."""

import csv
import hashlib
import json
from pathlib import Path

from src.expression_context.dmt_adc import index_for_gene


ROOT = Path(__file__).resolve().parent.parent


def test_dmt_index_covers_curator_gene_labels_and_maps_erbb2():
    data = json.loads((ROOT / "src/expression_context/dmt_adc_index.json").read_text())
    source = ROOT / ".polly-input/DMT_ADC_Compiled_List.csv"
    if source.exists():
        assert hashlib.sha256(source.read_bytes()).hexdigest() == data["source"]["csv_sha256"]
        with source.open(newline="") as handle:
            source_genes = {row["Gene"] for row in csv.DictReader(handle)}
        assert len(source_genes) == 99
    assert data["version"] == 1
    assert data["source"]["rows"] == 221
    assert data["source"]["unresolved_genes"] == ["IGTA10"]
    assert len(data["genes"]) == 98
    erbb2 = index_for_gene("ERBB2")
    assert {code for row in erbb2 for code in row["tcga_codes"]} >= {"BRCA", "BLCA"}
    assert index_for_gene("MISSING_GENE") == []
    assert any(row["tissue_type"] == "SCLC" and not row["tcga_codes"]
               for row in index_for_gene("DLL3"))
    assert data["tissue_to_tcga"]["Lung/solid?"] == ["LUAD", "LUSC"]
