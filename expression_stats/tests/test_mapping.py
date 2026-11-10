import pytest

from gene_expression_relevance import mapping


def test_metadata_drives_code_and_name():
    row = mapping.map_study(
        dict(
            studyId="brca_tcga_pan_can_atlas_2018",
            cancerTypeId="brca",
            name="API name",
            importDate="2026-06-05 15:34:03",
        )
    )
    assert row == dict(
        study_id="brca_tcga_pan_can_atlas_2018",
        oncotree_code="BRCA",
        name="API name",
        import_date="2026-06-05 15:34:03",
        tumor_type_query="breast cancer",
    )


def test_combined_colorectal():
    row = mapping.map_study(
        dict(
            studyId="coadread_tcga_pan_can_atlas_2018",
            cancerTypeId="coad",
            name="Colorectal",
            importDate="today",
        )
    )
    assert row["oncotree_code"] == "COADREAD"
    assert row["tumor_type_query"] == "colorectal cancer"


def test_all_32_have_queries():
    assert len(mapping.OVERRIDES) == 32
    assert "coad" not in mapping.OVERRIDES and "read" not in mapping.OVERRIDES


def test_unknown_mapping_fails():
    with pytest.raises(ValueError, match="Missing query"):
        mapping.map_study(dict(studyId="unknown", cancerTypeId="new"))


def test_mocked_api(monkeypatch):
    calls = []

    def get(url):
        calls.append(url)
        return dict(
            studyId=url.split("/")[-1], cancerTypeId="brca", name="Breast", importDate="today"
        )

    monkeypatch.setattr(mapping.http, "get_json", get)
    assert mapping.cancer_mapping(["brca_tcga_pan_can_atlas_2018"])[0]["oncotree_code"] == "BRCA"
    assert calls == ["https://www.cbioportal.org/api/studies/brca_tcga_pan_can_atlas_2018"]


@pytest.mark.parametrize(
    "study,cancer,expected",
    [
        ("lihc", "hcc", "LIHC"),
        ("laml", "aml", "LAML"),
        ("gbm", "difg", "GBM"),
        ("lgg", "difg", "LGG"),
        ("sarc", "soft_tissue", "SARC"),
        ("pcpg", "soft_tissue", "PCPG"),
    ],
)
def test_api_category_overrides_preserve_study_partitions(study, cancer, expected):
    row = mapping.map_study(
        dict(
            studyId=study + "_tcga_pan_can_atlas_2018",
            cancerTypeId=cancer,
            name="API name",
            importDate="today",
        )
    )
    assert row["oncotree_code"] == expected
