from src.models.schema import LiteratureRecord
from src.pipeline.clinical_actionability import assess_clinical_actionability


def test_high_confidence_actionability_requires_verified_clinical_support():
    result = assess_clinical_actionability(
        gene="ALK",
        tumor_type="lung adenocarcinoma",
        in_oncokb=True,
        insufficient_evidence=False,
        citations=["1", "2"],
        records=[
            LiteratureRecord(
                pmid="1",
                title="ALK fusion lung adenocarcinoma responds to crizotinib",
                abstract=(
                    "Patients with lung adenocarcinoma harboring ALK fusion retained "
                    "the kinase domain and responded to crizotinib inhibitor therapy."
                ),
                journal="J Clin Oncol",
                publication_types=["Clinical Trial"],
            ),
            LiteratureRecord(
                pmid="2",
                title="ALK inhibitor response in lung adenocarcinoma",
                abstract=(
                    "An independent patient cohort with ALK rearrangement lung "
                    "adenocarcinoma showed response to alectinib targeted therapy."
                ),
                publication_types=["Journal Article"],
            ),
            LiteratureRecord(
                pmid="3",
                title="Uncited ALK review",
                abstract="This review mentions ALK inhibitor therapy.",
                publication_types=["Review"],
            ),
        ],
    )

    assert result is not None
    assert result.confidence_score == 1.0
    assert result.pmids == ["1", "2"]
    assert "crizotinib" in result.summary.lower()
    assert [component.code for component in result.score_components] == [
        "clinical_evidence",
        "direct_actionability_language",
        "same_tumor_type",
        "multiple_pmids",
        "high_impact_journal",
        "oncokb_corroboration",
    ]
    direct_component = result.score_components[1]
    assert direct_component.delta == 0.25
    assert direct_component.pmids == ["1", "2"]
    assert "crizotinib" in direct_component.detail.lower()
    assert "kinase domain" in direct_component.detail.lower()
    assert result.evidence[0].domains == ["kinase domain"]


def test_preclinical_only_actionability_stays_hidden_below_threshold():
    result = assess_clinical_actionability(
        gene="ALK",
        tumor_type="lung adenocarcinoma",
        in_oncokb=False,
        insufficient_evidence=False,
        citations=["1"],
        records=[
            LiteratureRecord(
                pmid="1",
                title="ALK fusion cell-line inhibitor sensitivity",
                abstract=(
                    "An ALK fusion cell line retained the kinase domain and showed "
                    "in vitro sensitivity to crizotinib inhibitor treatment."
                ),
                publication_types=["Journal Article"],
            ),
        ],
    )

    assert result is None


def test_review_only_actionability_stays_hidden():
    result = assess_clinical_actionability(
        gene="ALK",
        tumor_type="lung adenocarcinoma",
        in_oncokb=True,
        insufficient_evidence=False,
        citations=["1"],
        records=[
            LiteratureRecord(
                pmid="1",
                title="Review of ALK inhibitors",
                abstract="This review summarizes ALK fusion inhibitor therapy.",
                publication_types=["Review"],
            ),
        ],
    )

    assert result is None
