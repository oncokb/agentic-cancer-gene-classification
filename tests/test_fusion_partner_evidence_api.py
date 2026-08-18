from __future__ import annotations

from fastapi.testclient import TestClient

from src import main
from src.models.schema import FusionPartnerEvidenceResult


def test_fusion_partner_evidence_scopes_to_tumor_type_by_default(monkeypatch):
    seen = []

    async def fake_retrieve(gene, tumor_type=None, agnostic=False, exclude_pmids=None):
        seen.append((gene, tumor_type, agnostic, exclude_pmids))
        return FusionPartnerEvidenceResult(
            gene=gene,
            tumor_type=tumor_type,
            scope="tumor_type_scoped" if tumor_type and not agnostic else "all_tumor_types",
            has_precedent=True,
            retrieved_count=1,
            pmids=["1"],
            interpretation="ALK has precedent.",
        )

    monkeypatch.setattr(main, "retrieve_fusion_partner_evidence", fake_retrieve)
    client = TestClient(main.app)

    response = client.post(
        "/v1/fusion-partner-evidence",
        json={"gene": "alk", "tumor_type": "LUAD"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["gene"] == "ALK"
    assert payload["scope"] == "tumor_type_scoped"
    assert seen == [("ALK", "LUAD", False, set())]


def test_fusion_partner_evidence_without_tumor_type_is_agnostic(monkeypatch):
    seen = []

    async def fake_retrieve(gene, tumor_type=None, agnostic=False, exclude_pmids=None):
        seen.append((gene, tumor_type, agnostic))
        return FusionPartnerEvidenceResult(gene=gene, scope="all_tumor_types")

    monkeypatch.setattr(main, "retrieve_fusion_partner_evidence", fake_retrieve)
    client = TestClient(main.app)

    response = client.post("/v1/fusion-partner-evidence", json={"gene": "TUSC5"})

    assert response.status_code == 200
    assert seen == [("TUSC5", None, True)]


def test_fusion_partner_evidence_follow_up_passes_exclude_pmids(monkeypatch):
    seen = []

    async def fake_retrieve(gene, tumor_type=None, agnostic=False, exclude_pmids=None):
        seen.append(exclude_pmids)
        return FusionPartnerEvidenceResult(gene=gene, scope="all_tumor_types")

    monkeypatch.setattr(main, "retrieve_fusion_partner_evidence", fake_retrieve)
    client = TestClient(main.app)

    response = client.post(
        "/v1/fusion-partner-evidence",
        json={"gene": "ALK", "tumor_type": "LUAD", "agnostic": True, "exclude_pmids": ["1", "2"]},
    )

    assert response.status_code == 200
    assert seen == [{"1", "2"}]


def test_fusion_partner_evidence_rejects_blank_gene():
    client = TestClient(main.app)

    response = client.post("/v1/fusion-partner-evidence", json={"gene": "  "})

    assert response.status_code == 400
