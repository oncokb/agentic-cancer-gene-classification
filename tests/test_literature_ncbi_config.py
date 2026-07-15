"""Tests for local NCBI API key configuration."""

from src.models.schema import LiteratureRecord
from src.pipeline import literature
from src.pipeline.literature import (
    _ncbi_params,
    configured_ncbi_api_key,
    ncbi_config_status,
    retrieve_literature,
    save_ncbi_api_key,
)


def test_save_ncbi_api_key_configures_local_lookup(tmp_path, monkeypatch):
    monkeypatch.setenv("AGCG_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("src.pipeline.literature.settings.ncbi_api_key", "")

    response = save_ncbi_api_key(" local-key \n")
    status = ncbi_config_status()

    assert response.configured is True
    assert response.source == "local_upload"
    assert configured_ncbi_api_key() == "local-key"
    assert status.configured is True
    assert status.source == "local_upload"
    assert _ncbi_params({"db": "pubmed"})["api_key"] == "local-key"


def test_ncbi_environment_key_wins_over_local_lookup(tmp_path, monkeypatch):
    monkeypatch.setenv("AGCG_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("src.pipeline.literature.settings.ncbi_api_key", "env-key")

    save_ncbi_api_key("local-key")

    assert configured_ncbi_api_key() == "env-key"
    status = ncbi_config_status()
    assert status.configured is True
    assert status.source == "environment"


async def test_local_retrieval_skips_tier2_when_initial_records_have_low_signal(monkeypatch):
    tier2_called = False

    async def fake_tier1_retrieve(gene, fusions):
        return [
            LiteratureRecord(
                pmid="1",
                title="Unrelated developmental biology",
                abstract="No oncology context is present.",
            )
        ]

    async def fake_tier2_local_retrieve(*args, **kwargs):
        nonlocal tier2_called
        tier2_called = True
        return []

    monkeypatch.setattr(literature, "_tier1_retrieve", fake_tier1_retrieve)
    monkeypatch.setattr(literature, "_tier2_local_retrieve", fake_tier2_local_retrieve)
    monkeypatch.setattr(literature.settings, "min_papers_for_strong_association", 4)
    monkeypatch.setattr(literature.settings, "local_tier2_min_prefilter_score", 2)

    records, tier = await retrieve_literature("GENE", ["GENE::PARTNER"], local_mode=True)

    assert tier == 1
    assert [record.pmid for record in records] == ["1"]
    assert tier2_called is False
