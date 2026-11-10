"""Thread-safe, globally rate-limited PubMed counts persisted across builds."""

import hashlib
import json
import math
import os
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

from . import PARAMS, http

# Deliberately outside the package-source fingerprint: operational settings
# must not invalidate literature verdicts when only throughput changes.
RUNTIME_SETTINGS_PATH = Path(__file__).resolve().parents[1] / "runtime-settings.json"


class PrecheckBudgetExceeded(TimeoutError):
    """Incomplete build; committed SQLite counts are safe to resume next run."""


class Precheck:
    def __init__(
        self,
        cache,
        api_key=None,
        requests_per_second=None,
        time_budget_seconds=None,
        clock=time.monotonic,
        sleep=time.sleep,
        wall_clock=time.time,
        ttl_days=PARAMS["precheck_cache_ttl_days"],
    ):
        settings = json.loads(RUNTIME_SETTINGS_PATH.read_text())
        self.time_budget_seconds = float(
            time_budget_seconds
            if time_budget_seconds is not None
            else os.environ.get(
                "EXPRESSION_STATS_PRECHECK_TIME_BUDGET_SECONDS",
                settings["precheck_time_budget_seconds"],
            )
        )
        self.build_time_budget_seconds = float(settings["build_time_budget_seconds"])
        self.ttl_jitter_fraction = float(settings["precheck_ttl_jitter_fraction"])
        if (
            not math.isfinite(self.time_budget_seconds)
            or self.time_budget_seconds <= 0
            or not 0 < self.build_time_budget_seconds <= 315 * 60
            or not 0 <= self.ttl_jitter_fraction < 1
        ):
            raise ValueError("Invalid precheck time budget or TTL jitter settings")
        self.deadline = None
        self.stopped = threading.Event()
        self.path = cache / "pubmed.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS counts "
            "(key TEXT PRIMARY KEY, query TEXT NOT NULL, count INTEGER NOT NULL)"
        )
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(counts)")}
        if "fetched_at" not in columns:
            self.db.execute("ALTER TABLE counts ADD COLUMN fetched_at REAL NOT NULL DEFAULT 0")
        self.db.commit()
        self.wall_clock, self.ttl_seconds = wall_clock, ttl_days * 86400
        self.cache = {
            key: dict(query=query, count=count, fetched_at=fetched_at)
            for key, query, count, fetched_at in self.db.execute(
                "SELECT key, query, count, fetched_at FROM counts"
            )
        }
        self.api_key = os.environ.get("NCBI_API_KEY", "") if api_key is None else api_key
        rate = float(
            requests_per_second
            if requests_per_second is not None
            else os.environ.get(
                "EXPRESSION_STATS_PUBMED_REQUESTS_PER_SECOND",
                settings["precheck_requests_per_second"],
            )
        )
        limit = 10 if self.api_key else 3
        if not math.isfinite(rate) or not 0 < rate <= limit:
            raise ValueError(f"PubMed requests_per_second must be >0 and <= {limit}")
        self.requests_per_second = rate
        self.interval = 1 / rate + 0.001
        self.clock, self.sleep, self.last = clock, sleep, None

    def start_budget(self, *, build_deadline):
        self.deadline = min(self.clock() + self.time_budget_seconds, build_deadline)
        self.remaining()

    def remaining(self):
        remaining = float("inf") if self.deadline is None else self.deadline - self.clock()
        if self.stopped.is_set() or remaining <= 0:
            self.stopped.set()
            raise PrecheckBudgetExceeded(
                "PubMed time budget exhausted; build incomplete, resume cache"
            )
        return remaining

    def stop(self):
        self.stopped.set()

    def expires_at(self, key, fetched_at):
        fraction = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big") / (2**64 - 1)
        return fetched_at + self.ttl_seconds * (1 - self.ttl_jitter_fraction * fraction)

    def throttle(self):
        # One shared limiter for every worker and retry, without bursts.
        with self.lock:
            self.remaining()
            if self.last is not None:
                delay = max(0, self.interval - (self.clock() - self.last))
                self.sleep(min(delay, self.remaining()))
            self.remaining()
            self.last = self.clock()

    def count(self, gene, code, query):
        self.remaining()
        key = f"{gene}|{code}"
        with self.lock:
            cached = self.cache.get(key)
        # Query overrides can evolve: do not reuse an old phrase's count.
        term = f'"{gene}"[tiab] AND ({query})'
        if (
            cached is not None
            and cached["query"] == term
            and cached["fetched_at"]
            <= self.wall_clock()
            < self.expires_at(key, cached["fetched_at"])
        ):
            return cached["count"]
        params = dict(
            db="pubmed",
            retmode="json",
            retmax=0,
            term=term,
            tool="gene-expression-relevance",
        )
        if self.api_key:
            params["api_key"] = self.api_key
        result = http.get_json(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?" + urlencode(params),
            before=self.throttle,
            remaining=self.remaining,
        )
        count = int(result["esearchresult"]["count"])
        with self.lock:
            self.cache[key] = dict(query=term, count=count, fetched_at=self.wall_clock())
            with self.db:
                self.db.execute(
                    "INSERT OR REPLACE INTO counts (key, query, count, fetched_at) "
                    "VALUES (?, ?, ?, ?)",
                    (key, term, count, self.cache[key]["fetched_at"]),
                )
        return count
