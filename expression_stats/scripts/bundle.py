"""Create a release bundle without changing any per-gene wire document."""

import argparse
import gzip
import hashlib
import json
import shutil
import tempfile
from datetime import datetime
from pathlib import Path


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def bundle(artifacts, destination):
    artifacts, destination = Path(artifacts) / "v1", Path(destination)
    manifest = json.loads((artifacts / "manifest.json").read_text())
    snapshot = datetime.fromisoformat(manifest["source"]["generated_at"]).date()
    tag = f"expression-stats-{manifest['release_id']}-pubmed-{snapshot:%Y%m%d}"
    paths = sorted((artifacts / "genes").glob("*.json"))
    if not paths:
        raise ValueError("Empty gene bundle")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        staging = Path(temporary)
        compressed = staging / "genes.jsonl.gz"
        with compressed.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as out:
            for path in paths:
                data = path.read_bytes()
                gene = json.loads(data)
                if gene["release_id"] != manifest["release_id"]:
                    raise ValueError("Mismatched gene release_id")
                out.write(data.rstrip(b"\n") + b"\n")
        manifest.update(
            bundle_tag=tag,
            pubmed_snapshot_date=snapshot.isoformat(),
            gene_count=len(paths),
            sha256=sha256(compressed),
            size_bytes=compressed.stat().st_size,
        )
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        for path in artifacts.glob("*.json"):
            if path.name != "manifest.json":
                shutil.copyfile(path, staging / path.name)
        if destination.exists():
            shutil.rmtree(destination)
        shutil.move(staging, destination)
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("artifacts", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(json.dumps(bundle(args.artifacts, args.destination)))
