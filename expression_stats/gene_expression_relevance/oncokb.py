"""OncoKB mutation classification client and versioned local cache.

Queries OncoKB's REST API (/api/v1/info, /api/v1/utils/cancerGeneList, and batched
POST /api/v1/annotate/mutations/byProteinChange) for nonsynonymous mutations in OncoKB
cancer genes, caches responses locally by hashed OncoKB data version, enforces
strict per-query ID validation, stops cleanly before job deadlines with incremental
SQLite commits for resumable CI runs, and never exposes per-variant annotations or API
credentials to release artifacts or logs.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import random
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ONCOKB_BASE_URL = "https://www.oncokb.org/api/v1"
CLASSIFICATION_VERSION = "2"
BATCH_SIZE = 500
DEFAULT_REQUEST_TIMEOUT = 60.0
RUNTIME_SETTINGS_PATH = Path(__file__).resolve().parents[1] / "runtime-settings.json"

POSITIVE_ONCOGENIC = frozenset({"Oncogenic", "Likely Oncogenic", "Resistance"})
EXCLUDED_ONCOGENIC_STATUS = {
    "Likely Neutral": "neutral",
    "Neutral": "neutral",
    "Inconclusive": "inconclusive",
    "Predicted Oncogenic": "predicted_oncogenic",
}
EXCLUDED_STATUS_KEYS = ("predicted_oncogenic", "inconclusive", "neutral")
KNOWN_ONCOGENIC = POSITIVE_ONCOGENIC | frozenset(EXCLUDED_ONCOGENIC_STATUS) | {"Unknown"}
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


class OncoKBRetryError(OncoKBError):
    """Raised when transient OncoKB retries are exhausted; cached batches stay safe."""


class OncoKBBudgetExceeded(TimeoutError):
    """Raised when the job/OncoKB deadline is reached; completed batches remain cached."""


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


def _safe_excerpt(value: object, secret: str | None = None, *, limit: int = 80) -> str:
    """Redact any API token and truncate server-derived values before formatting."""
    raw = value if isinstance(value, str) else repr(value)
    redacted = redact_secret(raw, secret)
    if len(redacted) > limit:
        redacted = redacted[:limit] + "..."
    return repr(redacted)


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


def classify_oncogenic(oncogenic: object, *, token: str | None = None) -> str:
    """Map a validated OncoKB oncogenic label to its internal verdict status.

    Returns 'oncokb' for positive calls, 'predicted_oncogenic' / 'inconclusive' /
    'neutral' for curated non-counting calls (no fallback), or 'unknown' (fallback).
    Rejects missing, non-string, or unrecognized values to fail closed.
    """
    if not isinstance(oncogenic, str) or oncogenic not in KNOWN_ONCOGENIC:
        raise OncoKBError(
            f"Invalid OncoKB oncogenic classification: {_safe_excerpt(oncogenic, token)}"
        )
    if oncogenic in POSITIVE_ONCOGENIC:
        return "oncokb"
    if oncogenic in EXCLUDED_ONCOGENIC_STATUS:
        return EXCLUDED_ONCOGENIC_STATUS[oncogenic]
    return "unknown"


def parse_retry_after(value: str | None, *, now: float) -> float:
    """Parse an HTTP Retry-After header (delay seconds or HTTP-date) into delay >= 0."""
    if not value:
        return 0.0
    stripped = str(value).strip()
    if not stripped:
        return 0.0
    try:
        seconds = float(stripped)
    except ValueError:
        try:
            dt = parsedate_to_datetime(stripped)
        except (TypeError, ValueError):
            return 0.0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return max(0.0, dt.timestamp() - now)
    if not math.isfinite(seconds):
        return 0.0
    return max(0.0, seconds)


def _retry_delay(
    attempt: int,
    *,
    retry_after: str | None = None,
    now: float = 0.0,
    jitter: float | None = None,
) -> float:
    """Compute uncapped exponential backoff + jitter respecting Retry-After."""
    base = max(float(2**attempt), parse_retry_after(retry_after, now=now))
    pad = random.uniform(0.0, 0.5) if jitter is None else max(0.0, float(jitter))
    return base + pad


def _is_retryable_http(code: int) -> bool:
    return code == 429 or 500 <= code < 600


def request_json(
    url: str,
    *,
    payload: object | None = None,
    token: str | None = None,
    attempts: int = 5,
    timeout: float = DEFAULT_REQUEST_TIMEOUT,
    sleep: Callable[[float], None] = time.sleep,
    wall_clock: Callable[[], float] = time.time,
    remaining: Callable[[], float] | None = None,
    jitter: Callable[[], float] | None = None,
) -> object:
    """Perform a bounded HTTP GET/POST JSON request with uncapped Retry-After backoff."""
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

    def check_remaining() -> float:
        if remaining is None:
            return float("inf")
        rem_val = remaining()
        if rem_val <= 0:
            raise OncoKBBudgetExceeded(
                "OncoKB annotation time budget exhausted; build incomplete, resume cache"
            )
        return rem_val

    for attempt in range(attempts):
        rem = check_remaining()
        req = Request(url, data=body, headers=headers, method=method)
        req_timeout = min(timeout, rem) if math.isfinite(rem) else timeout
        try:
            with urlopen(req, timeout=req_timeout) as response:
                return json.load(response)
        except HTTPError as exc:
            code = exc.code
            safe_url = redact_secret(url, token)
            if code in (401, 403):
                raise OncoKBAuthError(
                    f"OncoKB API authentication failed (HTTP {code}) for {safe_url}; "
                    "verify ONCOKB_API_TOKEN"
                ) from None
            if not _is_retryable_http(code):
                raise OncoKBError(f"OncoKB API HTTP {code} for {safe_url}") from None
            rem_after = check_remaining()
            if attempt == attempts - 1:
                raise OncoKBRetryError(
                    f"OncoKB API HTTP {code} retries exhausted for {safe_url}"
                ) from None
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            delay = _retry_delay(
                attempt,
                retry_after=retry_after,
                now=wall_clock(),
                jitter=(jitter() if jitter is not None else None),
            )
        except (
            URLError,
            TimeoutError,
            http.client.HTTPException,
            json.JSONDecodeError,
            OSError,
        ) as exc:
            safe_url = redact_secret(url, token)
            rem_after = check_remaining()
            if attempt == attempts - 1:
                detail = _safe_excerpt(str(exc), token, limit=120)
                raise OncoKBRetryError(
                    f"OncoKB API request failed for {safe_url}: {type(exc).__name__}: {detail}"
                ) from None
            delay = _retry_delay(
                attempt,
                now=wall_clock(),
                jitter=(jitter() if jitter is not None else None),
            )
        if delay >= rem_after:
            raise OncoKBBudgetExceeded(
                f"OncoKB backoff ({delay:.1f}s) exceeds remaining job budget ({rem_after:.1f}s); "
                "build incomplete, resume cache"
            )
        sleep(delay)
    raise OncoKBRetryError(f"OncoKB API retries exhausted for {url}")


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
    """SQLite annotation cache keyed by hashed OncoKB data version (local-only)."""

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
        """Return cached classification labels for keys."""
        key_list = list(dict.fromkeys(keys))
        if not key_list:
            return {}
        found: dict[tuple[str, str, str, str], str] = {}
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
        """Persist a batch of validated OncoKB annotation records atomically."""
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


def version_cache_dir(cache_dir: Path, version: str) -> Path:
    """Collision-free cache directory keyed by SHA-256 of the exact dataVersion string."""
    digest = hashlib.sha256(version.encode("utf-8")).hexdigest()
    return Path(cache_dir) / "oncokb" / digest


def _atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def validate_batch_responses(
    queries: list[dict], responses: object, *, token: str | None = None
) -> dict[str, dict]:
    """Strictly match every batch response by unique query id and validate its oncogenic label.

    Never falls back to positional alignment; any missing, duplicate, unknown, malformed,
    or error entry raises OncoKBError before anything is cached.
    """
    if not isinstance(responses, list) or len(responses) != len(queries):
        raise OncoKBError(
            f"OncoKB batch response mismatch: sent {len(queries)}, got "
            f"{len(responses) if isinstance(responses, list) else type(responses).__name__}"
        )
    expected_ids = [str(item["id"]) for item in queries]
    expected_set = set(expected_ids)
    matched: dict[str, dict] = {}
    for item in responses:
        if not isinstance(item, dict):
            raise OncoKBError("Malformed OncoKB batch entry: expected JSON object")
        errors = item.get("errors")
        if errors:
            raise OncoKBError(
                f"OncoKB entry returned error(s): {_safe_excerpt(errors, token)}"
            )
        if item.get("error"):
            raise OncoKBError(
                f"OncoKB entry returned error: {_safe_excerpt(item.get('error'), token)}"
            )
        query_meta = item.get("query")
        qid = query_meta.get("id") if isinstance(query_meta, dict) else None
        if not isinstance(qid, str) or not qid:
            raise OncoKBError("OncoKB batch entry missing non-empty query.id")
        if qid not in expected_set:
            raise OncoKBError(
                f"OncoKB batch entry returned unexpected query.id {_safe_excerpt(qid, token)}"
            )
        if qid in matched:
            raise OncoKBError(
                f"OncoKB batch response contained duplicate query.id {_safe_excerpt(qid, token)}"
            )
        if "oncogenic" not in item:
            raise OncoKBError(
                f"OncoKB batch entry {_safe_excerpt(qid, token)} missing 'oncogenic' field"
            )
        classify_oncogenic(item["oncogenic"], token=token)
        matched[qid] = item
    if set(matched) != expected_set:
        missing = sorted(expected_set - set(matched))
        raise OncoKBError(
            "OncoKB batch response missing query.id(s): "
            f"{[_safe_excerpt(m, token) for m in missing[:5]]}"
        )
    return matched


class OncoKBClient:
    """Versioned OncoKB API client with incremental cache, strict validation, and deadline."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        token: str | None = None,
        base_url: str = ONCOKB_BASE_URL,
        requester: Callable[..., object] = request_json,
        batch_size: int = BATCH_SIZE,
        time_budget_seconds: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall_clock: Callable[[], float] = time.time,
        build_deadline: float | None = None,
    ) -> None:
        settings = json.loads(RUNTIME_SETTINGS_PATH.read_text())
        budget = float(
            time_budget_seconds
            if time_budget_seconds is not None
            else os.environ.get(
                "EXPRESSION_STATS_ONCOKB_TIME_BUDGET_SECONDS",
                settings.get("oncokb_time_budget_seconds", 14400),
            )
        )
        if not math.isfinite(budget) or budget <= 0:
            raise ValueError("Invalid OncoKB time budget setting")
        self.time_budget_seconds = budget
        self.clock = clock
        self.sleep = sleep
        self.wall_clock = wall_clock
        self.deadline: float | None = None

        self.base_url = base_url.rstrip("/")
        self.token = read_token(token)
        self._request = requester
        self.batch_size = max(1, int(batch_size))
        self.api_calls = 0
        self.unique_variants_queried = 0
        self.cache_hits = 0
        self.progress: dict[str, object] = {}

        # Enforce deadline before /info and /utils/cancerGeneList start-up requests.
        self.start_budget(build_deadline=build_deadline)

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

        version_dir = version_cache_dir(Path(cache_dir), version)
        version_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(
            version_dir / "version.json",
            {"data_version": version, "data_date": data_date},
        )
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

    def start_budget(self, *, build_deadline: float | None = None) -> None:
        own_deadline = self.clock() + self.time_budget_seconds
        self.deadline = (
            min(own_deadline, build_deadline) if build_deadline is not None else own_deadline
        )
        self.remaining()

    def remaining(self) -> float:
        if self.deadline is None:
            return float("inf")
        rem = self.deadline - self.clock()
        if rem <= 0:
            raise OncoKBBudgetExceeded(
                "OncoKB annotation time budget exhausted; build incomplete, resume cache"
            )
        return rem

    def _get(self, path: str, *, authenticated: bool) -> object:
        self.remaining()
        self.api_calls += 1
        result = self._request(
            f"{self.base_url}{path}",
            token=(self.token if authenticated else None),
            sleep=self.sleep,
            wall_clock=self.wall_clock,
            remaining=self.remaining,
        )
        self.remaining()
        return result

    def _post_batch(self, items: list[dict]) -> dict[str, dict]:
        self.api_calls += 1
        responses = self._request(
            f"{self.base_url}/annotate/mutations/byProteinChange",
            payload=items,
            token=self.token,
            sleep=self.sleep,
            wall_clock=self.wall_clock,
            remaining=self.remaining,
        )
        return validate_batch_responses(items, responses, token=self.token)

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
        if self.deadline is None:
            self.start_budget()
        started = self.clock()
        unique_keys = sorted(set(keys))
        if not unique_keys:
            self.verify_credentials()
            self.progress = dict(
                total=0, cached=0, annotated=0, remaining=0, batches=0, elapsed_seconds=0.0
            )
            return {}
        results = self.cache.get_many(unique_keys)
        cached_count = len(results)
        self.cache_hits += cached_count
        missing = [key for key in unique_keys if key not in results]
        total_missing = len(missing)
        self.progress = dict(
            total=len(unique_keys),
            cached=cached_count,
            annotated=0,
            remaining=total_missing,
            batches=0,
            elapsed_seconds=round(self.clock() - started, 2),
        )
        if not missing:
            self.verify_credentials()
            return results

        total_batches = (total_missing + self.batch_size - 1) // self.batch_size
        annotated_here = 0
        for batch_num, start in enumerate(range(0, total_missing, self.batch_size), 1):
            self.remaining()
            batch = missing[start : start + self.batch_size]
            queries = []
            for idx, (symbol, alteration, consequence, build) in enumerate(batch):
                query: dict[str, object] = {
                    "id": f"b{batch_num}-{idx}",
                    "gene": {"hugoSymbol": symbol},
                    "alteration": alteration,
                    "referenceGenome": build,
                }
                if consequence and consequence in ALLOWED_CONSEQUENCES:
                    query["consequence"] = consequence
                queries.append(query)
            matched = self._post_batch(queries)
            self._auth_verified = True
            db_rows: list[tuple[str, str, str, str, str, str, int, int]] = []
            batch_verdicts: dict[tuple[str, str, str, str], str] = {}
            for idx, key in enumerate(batch):
                symbol, alteration, consequence, build = key
                qid = str(queries[idx]["id"])
                item = matched[qid]
                oncogenic = item["oncogenic"]
                verdict = classify_oncogenic(oncogenic, token=self.token)
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
                batch_verdicts[key] = verdict
            self.cache.put_many(db_rows)
            results.update(batch_verdicts)
            annotated_here += len(batch)
            self.unique_variants_queried += len(batch)
            remaining_count = total_missing - annotated_here
            elapsed = self.clock() - started
            self.progress = dict(
                total=len(unique_keys),
                cached=cached_count,
                annotated=annotated_here,
                remaining=remaining_count,
                batches=batch_num,
                elapsed_seconds=round(elapsed, 2),
            )
            print(
                f"OncoKB annotated {annotated_here}/{total_missing} variants "
                f"({remaining_count} remaining, batch {batch_num}/{total_batches}, "
                f"{elapsed:.1f}s)",
                flush=True,
            )
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
    build_deadline: float | None = None,
) -> OncoKBClient:
    """Factory hook (monkeypatchable in tests) creating a live OncoKBClient."""
    return OncoKBClient(
        cache_dir, token=token, base_url=base_url, build_deadline=build_deadline
    )
