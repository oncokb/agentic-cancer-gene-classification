"""Exit before data downloads if a fresh matching build is published."""

import json
import os
import re
import runpy
import subprocess
from datetime import UTC, datetime
from pathlib import Path

PROVENANCE = runpy.run_path("gene_expression_relevance/provenance.py")
SETTINGS = runpy.run_path("gene_expression_relevance/__init__.py")


def unchanged(manifest, commit, method, genes, skip, code_sha, hgnc_sha, now=None):
    source = manifest.get("source", {})
    if not (
        manifest.get("schema_version") == "1"
        and source.get("datahub_commit") == commit
        and manifest.get("method_version") == method
        and source.get("code_sha256") == code_sha
        and source.get("hgnc_sha256") == hgnc_sha
        and manifest.get("params") == SETTINGS["PARAMS"]
        and manifest.get("alteration_method_version") == SETTINGS["ALTERATION_METHOD_VERSION"]
        and manifest.get("alteration_params") == SETTINGS["ALTERATION_PARAMS"]
        and manifest.get("build")
        == {"genes": genes, "skip_precheck": skip, "skip_alterations": False}
    ):
        return False
    if not skip:
        try:
            generated = datetime.fromisoformat(source["generated_at"])
            age = ((now or datetime.now(UTC)) - generated).total_seconds()
            fresh = 0 <= age < SETTINGS["PARAMS"]["precheck_cache_ttl_days"] * 86400
            if "precheck_expires_at" in source:
                fresh = fresh and (now or datetime.now(UTC)) < datetime.fromisoformat(
                    source["precheck_expires_at"]
                )
            return fresh
        except (KeyError, TypeError, ValueError):
            return False
    return True


def current_mapping_sha(commit):
    from gene_expression_relevance.fetch import studies
    from gene_expression_relevance.mapping import cancer_mapping

    return PROVENANCE["mapping_fingerprint"](cancer_mapping(studies(commit)))


def published_manifest():
    pages = json.loads(
        subprocess.check_output(
            ["gh", "api", "--paginate", "--slurp", "repos/{owner}/{repo}/releases"], text=True
        )
    )
    releases = [
        r
        for page in pages
        for r in page
        if not r["draft"] and r["tag_name"].startswith("expression-stats-")
    ]
    if not releases:
        return {}
    latest = max(releases, key=lambda r: r["published_at"])
    asset = next(a for a in latest["assets"] if a["name"] == "manifest.json")
    return json.loads(
        subprocess.check_output(
            [
                "gh",
                "api",
                "-H",
                "Accept: application/octet-stream",
                f"repos/{{owner}}/{{repo}}/releases/assets/{asset['id']}",
            ],
            text=True,
        )
    )


def main():
    commit = (
        os.environ.get("INPUT_COMMIT", "").strip() or Path("datahub-commit.txt").read_text().strip()
    )
    if not re.fullmatch("[0-9a-f]{40}", commit):
        raise ValueError("Expected a full datahub commit SHA")
    genes = sorted(
        {s.strip().upper() for s in os.environ.get("INPUT_GENES", "").split(",") if s.strip()}
    )
    skip = os.environ.get("INPUT_SKIP", "").lower() == "true"
    code_sha = PROVENANCE["code_fingerprint"]()
    hgnc_sha = PROVENANCE["hgnc_pin"]()["sha256"]
    manifest = published_manifest()
    # Branch dispatches, subsets and skipped prechecks always build review artifacts.
    on_main = os.environ.get("GITHUB_REF", "refs/heads/main") == "refs/heads/main"
    deployable = on_main and not genes and not skip
    changed = not deployable or not unchanged(
        manifest, commit, SETTINGS["METHOD_VERSION"], None, skip, code_sha, hgnc_sha
    )
    if not changed:
        changed = current_mapping_sha(commit) != manifest.get("source", {}).get(
            "cohort_mapping_sha256"
        )
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(
            f"commit={commit}\nchanged={str(changed).lower()}\n"
            f"deployable={str(deployable).lower()}\nhgnc_sha={hgnc_sha}\n"
        )


if __name__ == "__main__":
    main()
