"""Publish only complete builds, as verified draft-first GitHub releases."""

import argparse
import gzip
import json
import os
import runpy
import subprocess
import tempfile
from pathlib import Path

from bundle import sha256

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PANEL_PATH = PROJECT_ROOT / "controls.yaml"
# This helper is stdlib-only; the publisher installs no stats dependencies.
CONTROLS = runpy.run_path(str(PROJECT_ROOT / "gene_expression_relevance/controls.py"))
expected_counts = CONTROLS["expected_counts"]
expected_alteration_counts = CONTROLS["expected_alteration_counts"]
control_genes = CONTROLS["control_genes"]
recheck_controls = CONTROLS["recheck"]


def bundled_documents(payload, genes):
    """Per-gene documents for `genes` from genes.jsonl.gz (absent genes are omitted)."""
    documents = {}
    with gzip.open(payload, "rt", encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            document = json.loads(line)
            symbol = document["gene"]["hugo_symbol"]
            if symbol in genes:
                if symbol in documents:
                    raise ValueError(f"Duplicate gene in bundle: {symbol}")
                documents[symbol] = document
    return documents


def gh(*args):
    return subprocess.check_output(["gh", *args], text=True)


def publish(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest["build"] != {"genes": None, "skip_precheck": False, "skip_alterations": False}:
        raise ValueError(
            "Only full builds with PubMed precheck and alteration statistics can be published"
        )
    if "precheck_expires_at" not in manifest["source"] or manifest["source"].get(
        "precheck_incomplete"
    ):
        raise ValueError("Missing completed PubMed precheck")
    report_path = directory / "controls-report.json"
    report = json.loads(report_path.read_text())
    controls = manifest.get("controls", {})
    panel_hash = sha256(PANEL_PATH)
    panel = json.loads(PANEL_PATH.read_text())
    positive_total, negative_total = expected_counts(panel)
    codes = [item["oncotree_code"] for item in manifest["cancer_types"]]
    alteration_positives, alteration_negatives = expected_alteration_counts(panel, codes)
    alterations = report.get("alterations", {})
    if (
        controls.get("status") != "passed"
        or report.get("status") != "passed"
        or controls.get("controls_sha256") != panel_hash
        or report.get("controls_sha256") != panel_hash
        or report.get("positive_passed") != positive_total
        or report.get("positive_total") != positive_total
        or report.get("negative_passed") != 0
        or report.get("negative_total") != negative_total
        or report.get("missing") != 0
        or report.get("failures") != []
        or alterations.get("status") != "passed"
        or controls.get("alterations", {}).get("status") != "passed"
        or alterations.get("positive_passed") != alteration_positives
        or alterations.get("positive_total") != alteration_positives
        or alterations.get("negative_passed") != 0
        or alterations.get("negative_total") != alteration_negatives
        or alterations.get("missing") != 0
        or alterations.get("failures") != []
    ):
        raise ValueError("A complete passing controls report is required for publication")
    payload = directory / "genes.jsonl.gz"
    if sha256(payload) != manifest["sha256"] or payload.stat().st_size != manifest["size_bytes"]:
        raise ValueError("Bundle integrity mismatch")
    # Never trust copied counters: re-derive every control from the bundled gene results.
    documents = bundled_documents(payload, control_genes(panel))
    problems = recheck_controls(report, lambda gene: documents.get(gene), codes, panel)
    if problems:
        raise ValueError("Bundled gene results fail the control recheck: " + "; ".join(problems))
    tag = manifest["bundle_tag"]
    # Paginate: application releases and old bundles may exceed one API page.
    releases = json.loads(gh("api", "--paginate", "--slurp", "repos/{owner}/{repo}/releases"))
    for page in releases:
        for release in page:
            if release["tag_name"] == tag:
                if release["draft"]:
                    raise ValueError(f"stale draft {release['id']}; delete it")
                print(f"Release {tag} already exists; skipping")
                return
    name = f"Expression stats {manifest['release_id']} (PubMed {manifest['pubmed_snapshot_date']})"
    created = json.loads(
        gh(
            "api",
            "repos/{owner}/{repo}/releases",
            "-f",
            f"tag_name={tag}",
            "-f",
            f"target_commitish={os.environ['GITHUB_SHA']}",
            "-f",
            f"name={name}",
            "-F",
            "draft=true",
            "-F",
            "prerelease=false",
            "-f",
            "make_latest=false",
        )
    )
    try:
        assets = sorted(directory.iterdir())
        gh("release", "upload", tag, *(str(path) for path in assets))
        with tempfile.TemporaryDirectory() as temporary:
            gh("release", "download", tag, "--dir", temporary)
            for path in assets:
                downloaded = Path(temporary) / path.name
                if downloaded.stat().st_size != path.stat().st_size or sha256(downloaded) != sha256(
                    path
                ):
                    raise ValueError(f"Downloaded asset integrity mismatch: {path.name}")
    except Exception:
        # Delete only the draft this invocation created; preserve the original error.
        try:
            gh("api", "--method", "DELETE", f"repos/{{owner}}/{{repo}}/releases/{created['id']}")
        except Exception as cleanup_error:
            print(f"Could not delete draft {created['id']}: {cleanup_error}")
        raise
    # Only verified assets can become discoverable.
    gh(
        "api",
        "--method",
        "PATCH",
        f"repos/{{owner}}/{{repo}}/releases/{created['id']}",
        "-F",
        "draft=false",
        "-F",
        "prerelease=false",
        "-f",
        "make_latest=false",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("directory", type=Path)
    publish(parser.parse_args().directory)
