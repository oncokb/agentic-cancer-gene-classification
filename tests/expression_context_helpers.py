"""Shared fixtures/fakes for tests/test_expression_context_*.py.

The fixture *bundle* is built from tests/fixtures/expression_context/ (the
release manifest, genes/*.json and aliases.json) exactly as the producer
publishes it: ``genes.jsonl.gz`` (one gene document per line) plus a
``manifest.json`` carrying bundle_tag, pubmed_snapshot_date, gene_count,
sha256 and size_bytes. GitHub is mocked with pytest-httpx; nothing touches the
network.
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from src.expression_context import lease as lease_module
from src.expression_context.bundle_store import BundleRecord, BundleState, InMemoryBundleStore
from src.expression_context.catalog import StatsCatalog
from src.expression_context.lease import VerdictLease
from src.expression_context.models import JobRecord, LiteratureVerdict
from src.expression_context.redis_client import ModuleRedis
from src.expression_context.service import ExpressionContextService
from src.expression_context.settings import ExpressionContextSettings
from src.expression_context.store import InMemoryExpressionContextStore
from src.job_store import JobStore
from tests.conftest import FakeRedis

FIXTURES = Path(__file__).parent / "fixtures" / "expression_context"
RELEASE_ID = "datahub-abc1234def56-m2-b7e9f01a2c3d"
PUBMED_DATE = date(2026, 11, 2)
REPO = "oncokb/agentic-cancer-gene-classification"
GITHUB_API = "https://api.github.com"
LIST_URL = f"{GITHUB_API}/repos/{REPO}/releases?per_page=100"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def manifest(**overrides) -> dict:
    data = load_fixture("manifest.json")
    data.update(overrides)
    return data


def gene_file(symbol: str = "ERBB2", **overrides) -> dict:
    data = copy.deepcopy(load_fixture(f"genes/{symbol}.json"))
    data.update(overrides)
    return data


def aliases() -> dict:
    return load_fixture("aliases.json")


def bundle_tag(release_id: str = RELEASE_ID, pubmed: date = PUBMED_DATE) -> str:
    return f"expression-stats-{release_id}-pubmed-{pubmed.strftime('%Y%m%d')}"


def asset_url(tag: str, name: str) -> str:
    return f"https://github.com/{REPO}/releases/download/{tag}/{name}"


@dataclass
class FixtureBundle:
    tag: str
    manifest: Dict[str, Any]
    genes: List[Dict[str, Any]]
    genes_gz: bytes
    aliases: Optional[Dict[str, str]]
    published_at: str

    @property
    def release_id(self) -> str:
        return self.manifest["release_id"]


def build_bundle(
    genes: Iterable[str] = ("ERBB2", "MYCN"),
    *,
    pubmed: date = PUBMED_DATE,
    release_id: str = RELEASE_ID,
    published_at: str = "2026-11-02T06:00:00Z",
    edit_gene: Optional[Callable[[Dict[str, Any]], None]] = None,
    manifest_overrides: Optional[Dict[str, Any]] = None,
    with_aliases: bool = True,
    gz_bytes: Optional[bytes] = None,
) -> FixtureBundle:
    docs = []
    for symbol in genes:
        doc = gene_file(symbol, release_id=release_id)
        if edit_gene is not None:
            edit_gene(doc)
        docs.append(doc)
    jsonl = "".join(json.dumps(doc, separators=(",", ":")) + "\n" for doc in docs).encode()
    genes_gz = gz_bytes if gz_bytes is not None else gzip.compress(jsonl, mtime=0)
    tag = bundle_tag(release_id, pubmed)
    data = manifest(release_id=release_id)
    data.update(
        bundle_tag=tag,
        pubmed_snapshot_date=pubmed.isoformat(),
        gene_count=len(docs),
        sha256=hashlib.sha256(genes_gz).hexdigest(),
        size_bytes=len(genes_gz),
    )
    data.update(manifest_overrides or {})
    return FixtureBundle(
        tag=data["bundle_tag"] if "bundle_tag" in (manifest_overrides or {}) else tag,
        manifest=data,
        genes=docs,
        genes_gz=genes_gz,
        aliases=aliases() if with_aliases else None,
        published_at=published_at,
    )


def release_json(
    bundle: FixtureBundle,
    *,
    draft: bool = False,
    prerelease: bool = False,
    omit_assets: Tuple[str, ...] = (),
    genes_asset_size: Optional[int] = None,
) -> Dict[str, Any]:
    assets = [
        ("manifest.json", len(json.dumps(bundle.manifest))),
        ("genes.jsonl.gz", genes_asset_size if genes_asset_size is not None else len(bundle.genes_gz)),
    ]
    if bundle.aliases is not None:
        assets.append(("aliases.json", len(json.dumps(bundle.aliases))))
    return {
        "tag_name": bundle.tag,
        "name": f"Expression stats {bundle.release_id}",
        "draft": draft,
        "prerelease": prerelease,
        "published_at": bundle.published_at,
        "html_url": f"https://github.com/{REPO}/releases/tag/{bundle.tag}",
        "assets": [
            {"name": name, "size": size, "browser_download_url": asset_url(bundle.tag, name)}
            for name, size in assets
            if name not in omit_assets
        ],
    }


APP_RELEASE = {
    "tag_name": "v0.4.5", "draft": False, "prerelease": False,
    "published_at": "2026-11-03T15:49:55Z", "assets": [],
}


def add_assets(httpx_mock, bundle: FixtureBundle, *, reusable: bool = True) -> None:
    httpx_mock.add_response(
        url=asset_url(bundle.tag, "manifest.json"), json=bundle.manifest, is_reusable=reusable
    )
    httpx_mock.add_response(
        url=asset_url(bundle.tag, "genes.jsonl.gz"), content=bundle.genes_gz, is_reusable=reusable
    )
    if bundle.aliases is not None:
        httpx_mock.add_response(
            url=asset_url(bundle.tag, "aliases.json"), json=bundle.aliases, is_reusable=reusable
        )


def add_release_list(httpx_mock, releases: List[Dict[str, Any]], *, reusable: bool = True) -> None:
    httpx_mock.add_response(url=LIST_URL, json=releases, is_reusable=reusable)


def add_bundle_release(httpx_mock, bundle: FixtureBundle, *, reusable: bool = True) -> None:
    """GitHub lists one app release and the bundle; the bundle's assets download."""
    add_release_list(httpx_mock, [APP_RELEASE, release_json(bundle)], reusable=reusable)
    add_assets(httpx_mock, bundle, reusable=reusable)


async def seed_bundle(bundles: InMemoryBundleStore, bundle: FixtureBundle) -> None:
    """Import and activate a fixture bundle directly (no HTTP, no loader)."""
    token = "seed"
    assert await bundles.acquire_loader_lock(token, 60)
    data = bundle.manifest
    await bundles.begin_load(BundleRecord(
        bundle_tag=bundle.tag, release_id=data["release_id"],
        schema_version=data["schema_version"], method_version=data["method_version"],
        pubmed_snapshot_date=date.fromisoformat(data["pubmed_snapshot_date"]),
        gene_count=data["gene_count"], sha256=data["sha256"], size_bytes=data["size_bytes"],
        manifest=data, status="loading", load_started_at=datetime.now(timezone.utc),
        published_at=datetime.fromisoformat(bundle.published_at.replace("Z", "+00:00")),
        release_url=f"https://github.com/{REPO}/releases/tag/{bundle.tag}",
    ), token)
    await bundles.add_genes(bundle.tag, [
        (doc["gene"]["hugo_symbol"].upper(), doc["gene"].get("entrez_gene_id"),
         json.dumps(doc).encode())
        for doc in bundle.genes
    ], token)
    symbols = {doc["gene"]["hugo_symbol"].upper() for doc in bundle.genes}
    await bundles.add_aliases(bundle.tag, [
        (alias.upper(), target.upper())
        for alias, target in (bundle.aliases or {}).items()
        if target.upper() in symbols
    ], token)
    await bundles.finish_load(bundle.tag, len(bundle.genes), datetime.now(timezone.utc), token)
    assert await bundles.activate(bundle.tag, datetime.now(timezone.utc), token)
    await bundles.release_loader_lock(token)


def seeded_bundles(bundle: Optional[FixtureBundle] = None) -> InMemoryBundleStore:
    """A new in-memory bundle store with the fixture bundle active (synchronous)."""
    bundle = bundle or build_bundle()
    data = bundle.manifest
    now = datetime.now(timezone.utc)
    store = InMemoryBundleStore()
    store.bundles[bundle.tag] = BundleRecord(
        bundle_tag=bundle.tag, release_id=data["release_id"],
        schema_version=data["schema_version"], method_version=data["method_version"],
        pubmed_snapshot_date=date.fromisoformat(data["pubmed_snapshot_date"]),
        gene_count=data["gene_count"], sha256=data["sha256"], size_bytes=data["size_bytes"],
        manifest=data, status="ready", load_started_at=now, loaded_rows=len(bundle.genes),
        finished_at=now,
    )
    symbols = set()
    for doc in bundle.genes:
        symbol = doc["gene"]["hugo_symbol"].upper()
        symbols.add(symbol)
        store.genes[(bundle.tag, symbol)] = (doc["gene"].get("entrez_gene_id"), json.dumps(doc).encode())
    for alias, target in (bundle.aliases or {}).items():
        if target.upper() in symbols:
            store.aliases[(bundle.tag, alias.upper())] = target.upper()
    store.state = BundleState(active_bundle_tag=bundle.tag, activated_at=now)
    return store


def make_settings(**overrides) -> ExpressionContextSettings:
    values: Dict[str, Any] = dict(
        enabled=True,
        http_retry_backoff_seconds=0,
        http_retry_attempts=3,
        active_bundle_cache_seconds=0,
    )
    values.update(overrides)
    return ExpressionContextSettings(_env_file=None, **values)


def make_service(
    *,
    gate,
    store=None,
    bundles: Optional[InMemoryBundleStore] = None,
    redis=None,
    concurrency: int = 4,
    lease_wait: float = 5.0,
    lease_ttl: float = 5.0,
    lease: Optional[VerdictLease] = None,
) -> ExpressionContextService:
    """One 'pod': its own service (in-flight map, semaphore, catalog cache)."""
    bundles = bundles if bundles is not None else seeded_bundles()
    redis = redis if redis is not None else LeaseFakeRedis()
    return ExpressionContextService(
        catalog=StatsCatalog(bundles, cache_seconds=0),
        gate=gate,
        store=store if store is not None else InMemoryExpressionContextStore(),
        jobs=JobStore("expression_context", JobRecord, redis_client_factory=lambda: None),
        literature_concurrency=concurrency,
        lease=lease or VerdictLease(ttl_seconds=lease_ttl, redis=module_redis(redis)),
        lease_wait_seconds=lease_wait,
        lease_poll_seconds=0.01,
        releases_repo=REPO,
    )


class FakeLiteratureGate:
    """Records calls; returns a scripted verdict per tumor_type_query."""

    name = "acgc_pipeline_v1"

    def __init__(self, verdicts: Optional[Dict[str, Tuple[bool, List[str]]]] = None) -> None:
        # tumor_type_query -> (insufficient_evidence, pmids)
        self.verdicts = verdicts if verdicts is not None else {
            "breast cancer": (False, ["12345678", "23456789"]),
            "gastric cancer": (True, ["34567890"]),
            "esophageal cancer": (False, []),
        }
        self.calls: List[Tuple[str, str]] = []
        self.fail_for: set[str] = set()

    async def evaluate(self, hugo_symbol: str, tumor_type_query: str) -> LiteratureVerdict:
        self.calls.append((hugo_symbol, tumor_type_query))
        if tumor_type_query in self.fail_for:
            raise RuntimeError("gate exploded")
        insufficient, pmids = self.verdicts.get(tumor_type_query, (True, []))
        return LiteratureVerdict(
            literature_gate=self.name,
            tumor_type_query=tumor_type_query,
            # Deliberately claim supported: the service must recompute it.
            supported=True,
            insufficient_evidence=insufficient,
            pmids=pmids,
            rationale=f"{hugo_symbol} in {tumor_type_query}",
            evidence_support_score=0.8,
            evaluated_at=datetime.now(timezone.utc),
        )


class LeaseFakeRedis(FakeRedis):
    """tests.conftest.FakeRedis plus the module's owner-safe lease scripts.

    ``race`` (an async callable) simulates another pod acting at the worst
    moment: for EVAL it runs just before the (atomic) script; for a plain GET
    of a lease key it runs right after the value was read, i.e. between a
    non-atomic implementation's ownership check and its mutation.
    """

    race = None

    async def _run_race(self):
        if self.race is not None:
            race, self.race = self.race, None
            await race()

    async def get(self, key):
        value = await super().get(key)
        if key.startswith(lease_module.LEASE_PREFIX):
            await self._run_race()
        return value

    async def eval(self, script, numkeys, *keys_and_args):
        if script not in (lease_module.RENEW_SCRIPT, lease_module.RELEASE_SCRIPT):
            return await super().eval(script, numkeys, *keys_and_args)
        await self._run_race()
        keys, args = keys_and_args[:numkeys], keys_and_args[numkeys:]
        current = self._live(keys[0])
        if current is None or current.decode() != str(args[0]):
            return 0
        if script == lease_module.RENEW_SCRIPT:
            self._store[keys[0]] = (current, self.now + int(args[1]) / 1000)
            return 1
        del self._store[keys[0]]
        return 1

    def expires_at(self, key):
        return self._store[key][1]


def module_redis(fake):
    return ModuleRedis(factory=lambda: fake)


class DownRedis:
    """Every command fails like an unreachable Redis."""

    async def set(self, *a, **k):
        raise ConnectionError("down")

    async def get(self, *a, **k):
        raise ConnectionError("down")

    async def eval(self, *a, **k):
        raise ConnectionError("down")

