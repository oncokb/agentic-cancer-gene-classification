"""Publish only complete builds, as verified draft-first GitHub releases."""

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

from bundle import sha256


def gh(*args):
    return subprocess.check_output(["gh", *args], text=True)


def publish(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest["build"] != {"genes": None, "skip_precheck": False}:
        raise ValueError("Only full builds with PubMed precheck can be published")
    if "precheck_expires_at" not in manifest["source"]:
        raise ValueError("Missing completed PubMed precheck")
    tag = manifest["bundle_tag"]
    # Paginate: application releases and old bundles may exceed one API page.
    releases = json.loads(gh("api", "--paginate", "--slurp", "repos/{owner}/{repo}/releases"))
    if any(release["tag_name"] == tag for page in releases for release in page):
        print(f"Release {tag} already exists; skipping")
        return
    payload = directory / "genes.jsonl.gz"
    if sha256(payload) != manifest["sha256"] or payload.stat().st_size != manifest["size_bytes"]:
        raise ValueError("Bundle integrity mismatch")
    name = f"Expression stats {manifest['release_id']} (PubMed {manifest['pubmed_snapshot_date']})"
    created = json.loads(
        gh(
            "api",
            "repos/{owner}/{repo}/releases",
            "-f",
            f"tag_name={tag}",
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
    # A failed upload/verification leaves a draft, never a discoverable partial release.
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
