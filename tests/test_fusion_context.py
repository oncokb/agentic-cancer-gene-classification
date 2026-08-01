"""Tests for Genome Nexus fusion-position context support."""

from src.pipeline.fusion_context import (
    annotate_fusion_position_contexts,
    annotate_fusion_positions,
    genome_nexus_position_context,
    parse_fusion_input,
    parsed_input_from_fields,
)


def test_parse_fusion_input_accepts_exon_metadata():
    parsed = parse_fusion_input("EML4::ALK,13,20")

    assert parsed.fusion == "EML4::ALK"
    assert parsed.five_gene == "EML4"
    assert parsed.three_gene == "ALK"
    assert parsed.five_exon == "13"
    assert parsed.three_exon == "20"


async def test_annotate_fusion_positions_uses_input_when_genome_nexus_unconfigured(monkeypatch):
    monkeypatch.setattr("src.pipeline.fusion_context.settings.fusion_annotation_api_base_url", "")
    monkeypatch.setattr("src.pipeline.fusion_context.settings.fusion_annotation_api_enabled", True)

    contexts = await annotate_fusion_positions(["EML4::ALK,13,20"])

    assert len(contexts) == 1
    assert contexts[0].source == "input"
    assert contexts[0].fusion == "EML4::ALK"
    assert contexts[0].five_prime.exon == "13"
    assert contexts[0].three_prime.exon == "20"


async def test_annotate_fusion_positions_posts_batch_request(monkeypatch):
    seen = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "results": [
                    {
                        "error": "unresolved transcript",
                    }
                ]
            }

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            seen["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json):
            seen["url"] = url
            seen["json"] = json
            return FakeResponse()

    monkeypatch.setattr("src.pipeline.fusion_context.settings.fusion_annotation_api_base_url", "http://gn.test")
    monkeypatch.setattr("src.pipeline.fusion_context.settings.fusion_annotation_api_enabled", True)
    monkeypatch.setattr("src.pipeline.fusion_context.settings.fusion_annotation_api_timeout_seconds", 12.0)
    monkeypatch.setattr("src.pipeline.fusion_context.httpx.AsyncClient", FakeAsyncClient)

    contexts = await annotate_fusion_positions(["EML4::ALK,13,20"])

    assert seen["url"] == "http://gn.test/api/annotate/batch"
    assert seen["timeout"] == 12.0
    assert seen["json"]["fusions"][0]["five_gene"] == "EML4"
    assert seen["json"]["fusions"][0]["three_gene"] == "ALK"
    assert seen["json"]["fusions"][0]["five_exon"] == 13
    assert seen["json"]["fusions"][0]["three_exon"] == 20
    assert contexts[0].source == "input"
    assert contexts[0].error == "unresolved transcript"


def test_genome_nexus_position_context_extracts_breakpoints_domains_and_kinase():
    parsed = parse_fusion_input("EML4::ALK,13,20")
    item_result = {
        "result": {
            "interface": {
                "five_last_aa": 496,
                "three_first_aa": 1058,
                "domains": [
                    {
                        "gene": "ALK",
                        "name": "Protein kinase domain",
                        "start": 1116,
                        "end": 1383,
                        "status": "RETAINED",
                    },
                    {
                        "gene": "EML4",
                        "name": "WD repeat",
                        "start": 300,
                        "end": 420,
                        "status": "LOST",
                    },
                ],
            },
            "resolved": {
                "five": {
                    "gene": "EML4",
                    "transcript": "ENST00000318522",
                    "breakpoint": {
                        "cds_coord": 1488,
                        "genomic_position": 42491871,
                        "context": {"exon_rank": 13},
                    },
                    "structure": {"chrom": "chr2"},
                },
                "three": {
                    "gene": "ALK",
                    "transcript": "ENST00000389048",
                    "breakpoint": {
                        "cds_coord": 3172,
                        "genomic_position": 29446394,
                        "context": {"exon_rank": 20},
                    },
                    "structure": {"chrom": "chr2"},
                },
            },
            "knowledge": {
                "oncogenic": "Oncogenic",
                "therapies": ["Crizotinib", "Alectinib"],
                "evidence": [{"level": "A", "drug": "Crizotinib"}],
                "diseases": ["Non-small cell lung cancer"],
                "sources": ["CIViC"],
            },
        }
    }

    context = genome_nexus_position_context(parsed, item_result)

    assert context.source == "genome_nexus"
    assert context.five_prime.transcript_id == "ENST00000318522"
    assert context.three_prime.genomic_breakpoint == "chr2:29446394"
    assert context.three_prime.protein_breakpoint == "p.1058"
    assert context.five_prime.lost_domains == ["WD repeat (300-420)"]
    assert context.three_prime.retained_domains == ["Protein kinase domain (1116-1383)"]
    assert context.kinase_gene == "ALK"
    assert context.kinase_gene_side == "three_prime"
    assert context.kinase_domain_status == "retained"
    assert context.knowledge.oncogenic == "Oncogenic"
    assert context.knowledge.therapies == ["Crizotinib", "Alectinib"]
    assert context.knowledge.diseases == ["Non-small cell lung cancer"]
    assert context.knowledge.sources == ["CIViC"]


def test_genome_nexus_position_context_handles_missing_knowledge():
    parsed = parse_fusion_input("EML4::ALK,13,20")
    item_result = {
        "result": {
            "interface": {"five_last_aa": 496, "three_first_aa": 1058, "domains": []},
            "resolved": {
                "five": {"gene": "EML4"},
                "three": {"gene": "ALK"},
            },
        }
    }

    context = genome_nexus_position_context(parsed, item_result)

    assert context.knowledge is None


def test_parsed_input_from_fields_carries_genomic_breakpoints():
    parsed = parsed_input_from_fields(
        "EML4::ALK",
        five_genomic="chr2:42491871",
        three_genomic="chr2:29446394",
    )

    assert parsed.five_gene == "EML4"
    assert parsed.three_gene == "ALK"
    assert parsed.five_genomic == "chr2:42491871"
    assert parsed.three_genomic == "chr2:29446394"


async def test_annotate_fusion_position_contexts_uses_input_when_unconfigured(monkeypatch):
    monkeypatch.setattr("src.pipeline.fusion_context.settings.fusion_annotation_api_base_url", "")
    monkeypatch.setattr("src.pipeline.fusion_context.settings.fusion_annotation_api_enabled", False)

    parsed = parsed_input_from_fields("EML4::ALK", five_genomic="chr2:42491871")
    contexts = await annotate_fusion_position_contexts([parsed])

    assert len(contexts) == 1
    assert contexts[0].source == "input"
    assert contexts[0].five_prime.genomic_breakpoint == "chr2:42491871"
