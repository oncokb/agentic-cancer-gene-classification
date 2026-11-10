import hashlib
import io
import json
from http.client import HTTPException, IncompleteRead
from urllib.error import HTTPError

import pytest

from gene_expression_relevance import fetch, http

SHA = "a" * 40


def test_pinned_sha_required():
    for bad in ["master", "abc123", "A" * 40]:
        with pytest.raises(ValueError):
            fetch.validate_commit(bad)


def test_exact_32_discovery(monkeypatch):
    monkeypatch.setattr(
        fetch.http,
        "get_json",
        lambda url: {
            "tree": [
                {"path": "public", "type": "tree", "sha": "public-tree"},
                {"path": "brca" + fetch.SUFFIX, "type": "tree"},
            ]
        },
    )
    with pytest.raises(ValueError, match="Expected 32"):
        fetch.studies(SHA)


def test_oid_cache_across_commits_and_corrupt_repair(tmp_path, monkeypatch):
    content = b"Hugo_Symbol\tEntrez_Gene_Id\nALB\t213\n"
    oid = hashlib.sha256(content).hexdigest()
    pointer = f"version https://git-lfs.github.com/spec/v1\noid sha256:{oid}\nsize {len(content)}\n"
    monkeypatch.setattr(fetch, "studies", lambda c: ["lihc" + fetch.SUFFIX])
    monkeypatch.setattr(
        fetch.http,
        "request",
        lambda url: (
            (_ for _ in ()).throw(AssertionError("No eager .lfsconfig fetch"))
            if url.endswith(".lfsconfig")
            else io.BytesIO(pointer.encode())
        ),
    )
    calls = []

    def download(url, path, **kwargs):
        calls.append(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    monkeypatch.setattr(fetch.http, "download", download)
    paths = fetch.fetch_all(SHA, tmp_path)
    fetch.fetch_all("b" * 40, tmp_path)
    assert len(calls) == 1
    assert calls[0].startswith(
        f"https://media.githubusercontent.com/media/cBioPortal/datahub/{SHA}/"
    )
    next(iter(paths.values())).write_bytes(b"x" * len(content))
    fetch.fetch_all(SHA, tmp_path)
    assert len(calls) == 2


@pytest.mark.parametrize(
    "failure",
    [
        HTTPError("https://media.example", 404, "missing", {}, None),
        IncompleteRead(b"partial", 10),
        HTTPException("truncated"),
    ],
)
def test_lfs_custom_backend_fallback(tmp_path, monkeypatch, failure):
    content = b"expression"
    oid = hashlib.sha256(content).hexdigest()
    pointer = f"version https://git-lfs.github.com/spec/v1\noid sha256:{oid}\nsize {len(content)}\n"
    monkeypatch.setattr(fetch, "studies", lambda c: ["lihc" + fetch.SUFFIX])
    monkeypatch.setattr(
        fetch.http,
        "request",
        lambda url: io.BytesIO(
            b"[lfs]\nurl=https://custom-lfs.example/\n"
            if url.endswith(".lfsconfig")
            else pointer.encode()
        ),
    )

    def batch(url, **kwargs):
        assert url == "https://custom-lfs.example/objects/batch"
        assert json.loads(kwargs["data"])["objects"][0]["oid"] == oid
        return {"objects": [{"actions": {"download": {"href": "https://objects.example/file"}}}]}

    monkeypatch.setattr(fetch.http, "get_json", batch)

    def download(url, path, **kwargs):
        if "media.githubusercontent.com" in url:
            raise failure
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    monkeypatch.setattr(fetch.http, "download", download)
    fetch.fetch_all(SHA, tmp_path)


def test_retry_and_rate_hook(monkeypatch):
    calls, hooks, sleeps = [], [], []

    def open_request(request, **kwargs):
        calls.append(request)
        if len(calls) < 3:
            raise HTTPError(request.full_url, 429, "limited", {}, None)
        return io.BytesIO(b"ok")

    monkeypatch.setattr(http, "urlopen", open_request)
    monkeypatch.setattr(http.time, "sleep", sleeps.append)
    assert http.request("https://test.example", before=lambda: hooks.append(1)).read() == b"ok"
    assert len(hooks) == 3
    assert sleeps == [1, 2]


def test_download_retries_interrupted_body_and_cleans_partial(tmp_path, monkeypatch):
    from http.client import IncompleteRead

    calls = []

    class Broken(io.BytesIO):
        def read(self, size=-1):
            raise IncompleteRead(b"partial", 10)

    def request(*args, **kwargs):
        calls.append(1)
        return Broken() if len(calls) == 1 else io.BytesIO(b"complete")

    monkeypatch.setattr(http, "request", request)
    monkeypatch.setattr(http.time, "sleep", lambda seconds: None)
    target = tmp_path / "object"
    http.download("https://test.example", target)
    assert target.read_bytes() == b"complete"
    assert len(calls) == 2
    assert not target.with_suffix(".part").exists()


@pytest.mark.parametrize("error", [False, True])
def test_media_mismatch_uses_backend_and_reports_object_errors(tmp_path, monkeypatch, error):
    content = b"verified expression"
    oid = hashlib.sha256(content).hexdigest()
    pointer = f"version https://git-lfs.github.com/spec/v1\noid sha256:{oid}\nsize {len(content)}\n"
    monkeypatch.setattr(fetch, "studies", lambda commit: ["lihc" + fetch.SUFFIX])
    requested = []

    def request(url):
        requested.append(url)
        return io.BytesIO(
            b"[lfs]\nurl=https://custom.example\n"
            if url.endswith(".lfsconfig")
            else pointer.encode()
        )

    monkeypatch.setattr(fetch.http, "request", request)

    def batch(url, **kwargs):
        return {
            "objects": [
                dict(oid=oid, error=dict(code=404, message="missing"))
                if error
                else dict(oid=oid, actions=dict(download=dict(href="https://custom/file")))
            ]
        }

    monkeypatch.setattr(fetch.http, "get_json", batch)

    def download(url, path, **kwargs):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"wrong content" if "media.githubusercontent.com" in url else content)

    monkeypatch.setattr(fetch.http, "download", download)
    if error:
        with pytest.raises(ValueError, match="LFS backend error.*missing"):
            fetch.fetch_all(SHA, tmp_path)
    else:
        paths = fetch.fetch_all(SHA, tmp_path)
        assert next(iter(paths.values())).read_bytes() == content
    assert any(url.endswith(".lfsconfig") for url in requested)


def test_study_listing_uses_nonrecursive_git_trees(tmp_path, monkeypatch):
    from gene_expression_relevance.mapping import OVERRIDES

    calls = []

    def get(url):
        calls.append(url)
        if url.endswith(SHA):
            return {"tree": [dict(path="public", type="tree", sha="public-tree")]}
        return {"tree": [dict(path=prefix + fetch.SUFFIX, type="tree") for prefix in OVERRIDES]}

    monkeypatch.setattr(fetch.http, "get_json", get)
    assert len(fetch.studies(SHA)) == 32
    assert calls == [f"{fetch.API}/git/trees/{SHA}", f"{fetch.API}/git/trees/public-tree"]
