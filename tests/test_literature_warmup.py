from src.models.schema import LiteratureRecord, ResolvedGene
from src.pipeline import literature_warmup


def _resolved_gene(gene: str) -> ResolvedGene:
    return ResolvedGene(input_symbol=gene, canonical_symbol=gene, resolved=True)


async def test_warm_literature_cache_uses_tier1_only(monkeypatch):
    seen = []

    async def fake_normalize_fusions(inputs):
        assert inputs == ["TP53::BRAF"]
        return {
            "BRAF": (_resolved_gene("BRAF"), ["TP53::BRAF"]),
            "TP53": (_resolved_gene("TP53"), ["TP53::BRAF"]),
        }

    async def fake_tier1_retrieve(gene, fusions=None, tumor_type=None):
        seen.append((gene, fusions, tumor_type))
        return [LiteratureRecord(pmid=f"{gene}-1", title="Paper", abstract="Abstract")]

    monkeypatch.setattr(literature_warmup, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(literature_warmup, "_tier1_retrieve", fake_tier1_retrieve)

    report = await literature_warmup.warm_literature_cache(["TP53::BRAF"], concurrency=2)

    assert report["inputs_processed"] == 1
    assert report["genes_total"] == 2
    assert report["genes_warmed"] == 2
    assert report["genes_failed"] == 0
    assert sorted(item["gene"] for item in report["warmed"]) == ["BRAF", "TP53"]
    assert sorted(seen) == [
        ("BRAF", ["TP53::BRAF"], None),
        ("TP53", ["TP53::BRAF"], None),
    ]


async def test_warm_literature_cache_reports_gene_errors(monkeypatch):
    async def fake_normalize_fusions(_inputs):
        return {
            "BRAF": (_resolved_gene("BRAF"), ["TP53::BRAF"]),
            "TP53": (_resolved_gene("TP53"), ["TP53::BRAF"]),
        }

    async def fake_tier1_retrieve(gene, fusions=None, tumor_type=None):
        if gene == "TP53":
            raise RuntimeError("NCBI unavailable")
        return [LiteratureRecord(pmid="1", title="Paper", abstract="Abstract")]

    monkeypatch.setattr(literature_warmup, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(literature_warmup, "_tier1_retrieve", fake_tier1_retrieve)

    report = await literature_warmup.warm_literature_cache(["TP53::BRAF"], concurrency=1)

    assert report["genes_warmed"] == 1
    assert report["genes_failed"] == 1
    assert report["errors"] == [{"gene": "TP53", "error": "NCBI unavailable"}]
