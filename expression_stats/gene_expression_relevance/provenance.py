"""Build fingerprints shared by the CLI and CI using resolved dependency versions."""

import hashlib
import json
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).parent
PROJECT_ROOT = ROOT.parent


def hgnc_pin():
    return json.loads((ROOT / "hgnc-pin.json").read_text())


def fingerprint(paths):
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def code_fingerprint():
    paths = [p for p in ROOT.iterdir() if p.suffix in {".py", ".json", ".html", ".tsv"}]
    paths.extend(
        PROJECT_ROOT / name for name in ("pyproject.toml", "constraints.txt", "controls.yaml")
    )
    versions = {name: version(name) for name in ("numpy", "scipy", "pandas")}
    payload = dict(files_sha256=fingerprint(paths), resolved_versions=versions)
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def overrides_fingerprint():
    return fingerprint(ROOT.glob("*overrides.json"))


def mapping_fingerprint(mapping):
    fields = ("study_id", "oncotree_code", "name", "tumor_type_query")
    cohorts = sorted(
        ({key: item[key] for key in fields} for item in mapping), key=lambda item: item["study_id"]
    )
    return hashlib.sha256(
        json.dumps(cohorts, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def build_fingerprint(params, genes, skip, hgnc_sha, code_sha, mapping_sha, alterations=None):
    from .purity import SOURCE

    payload = dict(
        purity=SOURCE,
        params=params,
        genes=genes,
        skip_precheck=skip,
        hgnc_sha256=hgnc_sha,
        code_sha256=code_sha,
        cohort_mapping_sha256=mapping_sha,
        overrides_sha256=overrides_fingerprint(),
    )
    if alterations is not None:
        # Alteration method, params, driver list hash and every input object id.
        payload["alterations"] = alterations
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
