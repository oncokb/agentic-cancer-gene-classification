"""Pinned datahub discovery and content-addressed LFS downloads."""

import configparser
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPException
from pathlib import Path

from . import http

REPO = "https://raw.githubusercontent.com/cBioPortal/datahub"
API = "https://api.github.com/repos/cBioPortal/datahub"
FILE = "data_mrna_seq_v2_rsem.txt"
SUFFIX = "_tcga_pan_can_atlas_2018"


def validate_commit(commit):
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("datahub commit must be a full, lowercase 40-character SHA")


def studies(commit):
    validate_commit(commit)
    root = http.get_json(f"{API}/git/trees/{commit}")
    if root.get("truncated"):
        raise ValueError("Truncated datahub root tree")
    public = next(e["sha"] for e in root["tree"] if e["path"] == "public" and e["type"] == "tree")
    tree = http.get_json(f"{API}/git/trees/{public}")
    if tree.get("truncated"):
        raise ValueError("Truncated datahub public tree")
    result = sorted(
        e["path"] for e in tree["tree"] if e["type"] == "tree" and e["path"].endswith(SUFFIX)
    )
    if len(result) != 32 or f"coadread{SUFFIX}" not in result:
        raise ValueError(f"Expected 32 studies including COADREAD, found {len(result)}")
    return result


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fetch_all(commit, cache, workers=4):
    validate_commit(commit)
    if not 1 <= workers <= 8:
        raise ValueError("workers must be between 1 and 8")
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)

    def backend_download(oid, size, target):
        # Fetch configuration only if media failed or returned incorrect content.
        with http.request(f"{REPO}/{commit}/.lfsconfig") as response:
            config = configparser.ConfigParser()
            config.read_string(response.read().decode())
        backend = config.get("lfs", "url").rstrip("/")
        batch = http.get_json(
            f"{backend}/objects/batch",
            data=json.dumps(
                {
                    "operation": "download",
                    "transfers": ["basic"],
                    "objects": [{"oid": oid, "size": size}],
                }
            ).encode(),
            headers={
                "Content-Type": "application/vnd.git-lfs+json",
                "Accept": "application/vnd.git-lfs+json",
            },
        )
        obj = next((obj for obj in batch["objects"] if obj.get("oid", oid) == oid), None)
        if obj is None or "error" in obj:
            error = obj.get("error") if obj else "Object missing from batch response"
            raise ValueError(f"LFS backend error for {oid}: {error}")
        action = obj["actions"]["download"]
        http.download(action["href"], target, headers=action.get("header"))

    def verified(target, oid, size):
        return target.exists() and target.stat().st_size == size and digest(target) == oid

    def fetch(study):
        relative = f"public/{study}/{FILE}"
        with http.request(f"{REPO}/{commit}/{relative}") as response:
            pointer = response.read().decode()
        match = re.fullmatch(
            r"version https://git-lfs.github.com/spec/v1\noid sha256:([0-9a-f]{64})\n"
            r"size (\d+)\n?",
            pointer,
        )
        if not match:
            raise ValueError(f"Invalid LFS pointer: {study}")
        oid, size = match[1], int(match[2])
        target = cache / "objects" / oid
        if target.exists() and target.stat().st_size == size and digest(target) == oid:
            return study, target
        target.unlink(missing_ok=True)
        media = f"https://media.githubusercontent.com/media/cBioPortal/datahub/{commit}/{relative}"
        try:
            http.download(media, target)
        except (OSError, ValueError, HTTPException):
            target.unlink(missing_ok=True)
        if not verified(target, oid, size):
            target.unlink(missing_ok=True)
            backend_download(oid, size, target)
        if not verified(target, oid, size):
            target.unlink(missing_ok=True)
            raise ValueError(f"LFS integrity failure: {study}")
        print(f"Downloaded {study} ({size / 1e6:.1f} MB)", flush=True)
        return study, target

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(pool.map(fetch, studies(commit)))
