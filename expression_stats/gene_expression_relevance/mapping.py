"""Live public cBioPortal study metadata with explicit literature phrases."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import http

OVERRIDES = json.loads(Path(__file__).with_name("query_overrides.json").read_text())


CODE_OVERRIDES = json.loads(Path(__file__).with_name("cancer_code_overrides.json").read_text())


def map_study(metadata):
    study = metadata["studyId"]
    cancer = metadata["cancerTypeId"].lower()
    prefix = study.split("_")[0]
    override = CODE_OVERRIDES.get(prefix)
    if override and cancer in override["cancer_type_ids"]:
        code = override["code"]
    else:
        code = cancer
    if code not in OVERRIDES:
        raise ValueError(f"Missing query override for {study}: {code}")
    return dict(
        study_id=study,
        oncotree_code=code.upper(),
        name=metadata["name"],
        tumor_type_query=OVERRIDES[code],
        import_date=metadata["importDate"],
    )


def cancer_mapping(study_ids):
    def load(study):
        return map_study(http.get_json(f"https://www.cbioportal.org/api/studies/{study}"))

    with ThreadPoolExecutor(max_workers=4) as pool:
        result = list(pool.map(load, sorted(study_ids)))
    if len({row["oncotree_code"] for row in result}) != len(result):
        raise ValueError("Duplicate cancer type mapping")
    return result
