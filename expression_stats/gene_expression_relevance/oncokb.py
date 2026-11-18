"""OncoKB mutation classification client and versioned local cache.

Queries OncoKB's REST API (/api/v1/info, /api/v1/utils/cancerGeneList, and batched
POST /api/v1/annotate/mutations/byProteinChange) for nonsynonymous mutations in OncoKB
cancer genes, caches responses locally by OncoKB data version, and never exposes
per-variant annotations or API credentials to release artifacts or logs.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import random
import re
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ONCOKB_BASE_URL = "https://www.oncokb.org/api/v1"
CLASSIFICATION_VERSION = "1"
BATCH_SIZE = 500
DEFAULT_REQUEST_TIMEOUT = 60.0

POSITIVE_ONCOGENIC = frozenset({"Oncogenic", "Likely Oncogenic", "Predicted Oncogenic"})
NEGATIVE_ONCOGENIC = frozenset({"Likely Neutral", "Neutral"})
TSG_GENE_TYPES = frozenset({"TSG", "ONCOGENE_AND_TSG"})

ALLOWED_CONSEQUENCES = frozenset(
    {
        "feature_truncation",
        "frameshift_variant",
        "inframe_deletion",
        "inframe_insertion",
        "start_lost",
        "missense_variant",
        "splice_region_variant",
        "stop_gained",
        "synonymous_variant",
        "intron_variant",
    }
)
CONSEQUENCE_PRIORITY = (
    "stop_gained",
    "frameshift_variant",
    "inframe_deletion",
    "inframe_insertion",
    "start_lost",
    "missense_variant",
    "splice_region_variant",
    "feature_truncation",
)
VC_DEFAULT_CONSEQUENCE = {
    "Missense_Mutation": "missense_variant",
    "Nonsense_Mutation": "stop_gained",
    "Frame_Shift_Del": "frameshift_variant",
    "Frame_Shift_Ins": "frameshift_variant",
    "In_Frame_Del": "inframe_deletion",
    "In_Frame_Ins": "inframe_insertion",
    "Translation_Start_Site": "start_lost",
    "Splice_Site": "splice_region_variant",
}
_UNINFORMATIVE_ALTERATIONS = frozenset({"", ".", "?", "p.?", "="})


class OncoKBError(RuntimeError):
    """Raised when OncoKB API access or annotation fails in a required build."""


class OncoKBAuthError(OncoKBError):
    """Raised when ONCOKB_API_TOKEN is missing, invalid, or rejected (401/403)."""


def read_token(token: str | None = None) -> str:
    """Return a validated non-empty OncoKB API token from arg or environment."""
    value = (token if token is not None else os.environ.get("ONCOKB_API_TOKEN", "")).strip()
    if not value:
        raise OncoKBAuthError(
            "ONCOKB_API_TOKEN is required for OncoKB mutation annotation; "
            "set ONCOKB_API_TOKEN or pass --hotspot-only for a non-publishable review build."
        )
    return value


def redact_secret(text: str, secret: str | None) -> str:
    """Scrub any accidental occurrence of the API token from error text."""
    if not secret or not text:
        return text
    return text.replace(secret, "[REDACTED]")


def normalize_alteration(hgvsp: str) -> str:
    """Normalize MAF HGVSp_Short for OncoKB byProteinChange queries."""
    cleaned = str(hgvsp or "").strip()
    if cleaned in _UNINFORMATIVE_ALTERATIONS:
        return ""
    if cleaned.startswith("p."):
        cleaned = cleaned[2:].strip()
    if cleaned in _UNINFORMATIVE_ALTERATIONS:
        return ""
    return cleaned


def normalize_consequence(raw_consequence: str, variant_classification: str) -> str:
    """Map MAF Consequence / Variant_Classification to OncoKB's SO consequence enum."""
    terms = {
        part.strip()
        for part in str(raw_consequence or "").split(",")
        if part.strip()
    }
    for candidate in CONSEQUENCE_PRIORITY:
        if candidate in terms:
            return candidate
    if terms & {"splice_acceptor_variant", "splice_donor_variant"}:
        return "splice_region_variant"
    return VC_DEFAULT_CONSEQUENCE.get(str(variant_classification or ""), "")


def normalize_reference_genome(build: str) -> str:
    """Map MAF NCBI_Build to GRCh37 or GRCh38 (TCGA PanCan defaults to GRCh37)."""
    cleaned = str(build or "").strip().upper()
    if cleaned in {"GRCH38", "HG38", "38"}:
        return "GRCh38"
    return "GRCh37"


def classify_oncogenic(oncogenic: str | None) -> str:
    """Map an OncoKB oncogenic string to 'oncokb', 'neutral', or 'unknown'."""
    label = str(oncogenic or "").strip()
    if label in POSITIVE_ONCOGENIC:
        return "oncokb"
    if label in NEGATIVE_ONCOGENIC:
        return "neutral"
    return "unknown"


def _retry_delay(
    attempt: int, retry_after: str | None = None, jitter: float | None = None
) -> float:
    base = float(2**attempt)
    if retry_after:
        try:
            base = max(base, float(retry_after))
        except ValueError:
            pass
    pad = random.uniform(0.0, 0.5) if jitter is None else jitter
    return min(base + pad, 60.0)


def request_json(
    url: str,
    *,
    payload: object | None = None,
    token: str | None = None,
    attempts: int = 5,
    timeout: float = DEFAULT_REQUEST_TIMEOUT,
    sleep: Callable[[float], None] = time.sleep,
) -> object:
    """Perform a bounded HTTP GET/POST JSON request with retry and token redaction."""
    headers = {
        "Accept": "application/json",
        "User-Agent": "gene-expression-relevance/0.1",
    }
    body: bytes | None = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    method = "POST" if payload is not None else "GET"
    for attempt in range(attempts):
        req = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(req, timeout=timeout) as response:
                return json.load(response)
        except HTTPError as exc:
            code = exc.code
            if code in (401, 403):
                raise OncoKBAuthError(
                    f"OncoKB API authentication failed (HTTP {code}) for {url}; "
                    "verify ONCOKB_API_TOKEN"
                ) from None
            if code not in (408, 429, 500, 502, 503, 504) or attempt == attempts - 1:
                raise OncoKBError(f"OncoKB API HTTP {code} for {url}") from None
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            sleep(_retry_delay(attempt, retry_after=retry_after))
        except (
            URLError,
            TimeoutError,
            http.client.HTTPException,
            json.JSONDecodeError,
            OSError,
        ) as exc:
            if attempt == attempts - 1:
                detail = redact_secret(str(exc), token)
                raise OncoKBError(
                    f"OncoKB API request failed for {url}: {type(exc).__name__}: {detail}"
                ) from None
            sleep(_retry_delay(attempt))
    raise OncoKBError(f"OncoKB API retries exhausted for {url}")


@dataclass(frozen=True)
class CancerGeneIndex:
    """Lookup tables derived from /api/v1/utils/cancerGeneList."""

    data_version: str
    data_date: str | None
    sha256: str
    gene_types: dict[str, str]
    tsg_genes: frozenset[str]
    alias_to_symbol: dict[str, str]

    def resolve_symbol(self, symbol: str) -> str | None:
        """Map an HGNC symbol to an OncoKB cancer gene hugoSymbol, if tracked."""
        upper = str(symbol or "").strip().upper()
        if not upper:
            return None
        if upper in self.gene_types:
            return upper
        return self.alias_to_symbol.get(upper)


def build_cancer_gene_index(
    raw_genes: list[dict],
    data_version: str,
    data_date: str | None = None,
) -> CancerGeneIndex:
    """Normalize OncoKB cancerGeneList payload into lookup maps and canonical SHA-256."""
    normalized_rows = sorted(
        (
            {
                "hugoSymbol": str(item.get("hugoSymbol", "")).strip(),
                "entrezGeneId": item.get("entrezGeneId"),
                "geneType": item.get("geneType"),
                "geneAliases": sorted(
                    {str(alias).strip() for alias in (item.get("geneAliases") or []) if alias}
                ),
            }
            for item in raw_genes
            if item.get("hugoSymbol")
        ),
        key=lambda item: (item["hugoSymbol"], item["entrezGeneId"] or 0),
    )
    digest = hashlib.sha256(
        json.dumps(normalized_rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    gene_types: dict[str, str] = {}
    tsg_genes: set[str] = set()
    alias_candidates: dict[str, set[str]] = {}
    for row in normalized_rows:
        symbol = row["hugoSymbol"].upper()
        gene_type = str(row["geneType"] or "")
        gene_types[symbol] = gene_type
        if gene_type in TSG_GENE_TYPES:
            tsg_genes.add(symbol)
        for alias in row["geneAliases"]:
            alias_candidates.setdefault(alias.upper(), set()).add(symbol)
    # Only unambiguous aliases map to an OncoKB symbol; known primary symbols take priority.
    alias_to_symbol = {
        alias: next(iter(targets))
        for alias, targets in alias_candidates.items()
        if len(targets) == 1 and alias not in gene_types
    }
    return CancerGeneIndex(
        data_version=data_version,
        data_date=data_date,
        sha256=digest,
        gene_types=gene_types,
        tsg_genes=frozenset(tsg_genes),
        alias_to_symbol=alias_to_symbol,
    )


class OncoKBCache:
    """SQLite annotation cache keyed by OncoKB data version (local-only, never published)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, timeout=30.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS annotations (
                hugo_symbol TEXT NOT NULL,
                alteration TEXT NOT NULL,
                consequence TEXT NOT NULL,
                reference_genome TEXT NOT NULL,
                oncogenic TEXT NOT NULL,
                known_effect TEXT NOT NULL,
                variant_exist INTEGER NOT NULL,
                gene_exist INTEGER NOT NULL,
                PRIMARY KEY (hugo_symbol, alteration, consequence, reference_genome)
            )
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def get_many(
        self, keys: Iterable[tuple[str, str, str, str]]
    ) -> dict[tuple[str, str, str, str], str]:
        """Return cached classification labels ('oncokb'|'neutral'|'unknown') for keys."""
        key_list = list(dict.fromkeys(keys))
        if not key_list:
            return {}
        found: dict[tuple[str, str, str, str], str] = {}
        # Query in chunks within SQLite parameter limits.
        chunk_size = 200
        for start in range(0, len(key_list), chunk_size):
            chunk = key_list[start : start + chunk_size]
            placeholders = ",".join("(?,?,?,?)" for _ in chunk)
            flat = [value for row in chunk for value in row]
            cursor = self._conn.execute(
                f"""
                SELECT hugo_symbol, alteration, consequence, reference_genome, oncogenic
                FROM annotations
                WHERE (hugo_symbol, alteration, consequence, reference_genome)
                    IN (VALUES {placeholders})
                """,
                flat,
            )
            for sym, alt, conseq, build, oncogenic in cursor:
                found[(sym, alt, conseq, build)] = classify_oncogenic(oncogenic)
        return found

    def put_many(self, rows: list[tuple[str, str, str, str, str, str, int, int]]) -> None:
        """Persist a batch of OncoKB annotation records atomically."""
        if not rows:
            return
        with self._conn:
            self._conn.executemany(
                """
                INSERT OR REPLACE INTO annotations (
                    hugo_symbol, alteration, consequence, reference_genome,
                    oncogenic, known_effect, variant_exist, gene_exist
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )


def _safe_version_dir(version: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", version).strip("._") or "unknown"


def _atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


class OncoKBClient:
    """Versioned OncoKB API client with persistent local cache and retry/backoff."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        token: str | None = None,
        base_url: str = ONCOKB_BASE_URL,
        requester: Callable[..., object] = request_json,
        batch_size: int = BATCH_SIZE,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = read_token(token)
        self._request = requester
        self.batch_size = max(1, int(batch_size))
        self.api_calls = 0
        self.unique_variants_queried = 0
        self.cache_hits = 0

        info_payload = self._get("/info", authenticated=False)
        data_version_obj = (
            info_payload.get("dataVersion") if isinstance(info_payload, dict) else None
        ) or {}
        version = str(data_version_obj.get("version") or "").strip()
        if not version:
            raise OncoKBError("OncoKB /info response missing dataVersion.version")
        data_date = (
            str(data_version_obj["date"]).strip() if data_version_obj.get("date") else None
        )

        version_dir = Path(cache_dir) / "oncokb" / _safe_version_dir(version)
        version_dir.mkdir(parents=True, exist_ok=True)
        cgl_cache = version_dir / "cancer_gene_list.json"
        if cgl_cache.exists():
            raw_genes = json.loads(cgl_cache.read_text())
        else:
            raw_genes = self._get("/utils/cancerGeneList", authenticated=False)
            if not isinstance(raw_genes, list):
                raise OncoKBError("OncoKB /utils/cancerGeneList did not return a JSON list")
            _atomic_write_json(cgl_cache, raw_genes)

        self.index = build_cancer_gene_index(raw_genes, version, data_date)
        self.cache = OncoKBCache(version_dir / "annotations.sqlite3")
        self.cache_db_path = version_dir / "annotations.sqlite3"
        self._auth_verified = False

    def _get(self, path: str, *, authenticated: bool) -> object:
        self.api_calls += 1
        return self._request(
            f"{self.base_url}{path}",
            token=(self.token if authenticated else None),
        )

    def _post_batch(self, items: list[dict]) -> list[dict]:
        self.api_calls += 1
        response = self._request(
            f"{self.base_url}/annotate/mutations/byProteinChange",
            payload=items,
            token=self.token,
        )
        if not isinstance(response, list) or len(response) != len(items):
            raise OncoKBError(
                f"OncoKB batch response mismatch: sent {len(items)}, got "
                f"{len(response) if isinstance(response, list) else type(response).__name__}"
            )
        return response

    def verify_credentials(self) -> None:
        """Fail closed if ONCOKB_API_TOKEN is rejected even when variant cache is warm."""
        if self._auth_verified:
            return
        self._post_batch(
            [
                {
                    "id": "preflight",
                    "gene": {"hugoSymbol": "BRAF"},
                    "alteration": "V600E",
                    "consequence": "missense_variant",
                    "referenceGenome": "GRCh37",
                }
            ]
        )
        self._auth_verified = True

    def annotate(
        self, keys: Iterable[tuple[str, str, str, str]]
    ) -> dict[tuple[str, str, str, str], str]:
        """Classify unique (hugoSymbol, alteration, consequence, referenceGenome) keys."""
        unique_keys = sorted(set(keys))
        if not unique_keys:
            self.verify_credentials()
            return {}
        results = self.cache.get_many(unique_keys)
        self.cache_hits += len(results)
        missing = [key for key in unique_keys if key not in results]
        if not missing:
            self.verify_credentials()
            return results
        self.unique_variants_queried += len(missing)
        for start in range(0, len(missing), self.batch_size):
            batch = missing[start : start + self.batch_size]
            queries = []
            for idx, (symbol, alteration, consequence, build) in enumerate(batch):
                query: dict[str, object] = {
                    "id": str(idx),
                    "gene": {"hugoSymbol": symbol},
                    "alteration": alteration,
                    "referenceGenome": build,
                }
                if consequence and consequence in ALLOWED_CONSEQUENCES:
                    query["consequence"] = consequence
                queries.append(query)
            responses = self._post_batch(queries)
            self._auth_verified = True
            by_id: dict[str, dict] = {}
            for pos, item in enumerate(responses):
                qid = (item.get("query") or {}).get("id")
                by_id[str(qid) if qid is not None else str(pos)] = item
            db_rows: list[tuple[str, str, str, str, str, str, int, int]] = []
            for idx, key in enumerate(batch):
                symbol, alteration, consequence, build = key
                item = by_id.get(str(idx), responses[idx])
                oncogenic = str(item.get("oncogenic") or "Unknown")
                known_effect = str(
                    ((item.get("mutationEffect") or {}).get("knownEffect")) or ""
                )
                variant_exist = int(bool(item.get("variantExist")))
                gene_exist = int(bool(item.get("geneExist")))
                db_rows.append(
                    (
                        symbol,
                        alteration,
                        consequence,
                        build,
                        oncogenic,
                        known_effect,
                        variant_exist,
                        gene_exist,
                    )
                )
                results[key] = classify_oncogenic(oncogenic)
            self.cache.put_many(db_rows)
        return results

    def provenance(self) -> dict[str, object]:
        """Release-safe summary metadata (no per-variant data or credentials)."""
        return {
            "oncokb_data_version": self.index.data_version,
            "oncokb_data_date": self.index.data_date,
            "oncokb_classification_version": CLASSIFICATION_VERSION,
            "oncokb_cancer_gene_list_sha256": self.index.sha256,
        }

    def close(self) -> None:
        self.cache.close()


def load_client(
    cache_dir: Path,
    *,
    token: str | None = None,
    base_url: str = ONCOKB_BASE_URL,
) -> OncoKBClient:
    """Factory hook (monkeypatchable in tests) creating a live OncoKBClient."""
    return OncoKBClient(cache_dir, token=token, base_url=base_url)
