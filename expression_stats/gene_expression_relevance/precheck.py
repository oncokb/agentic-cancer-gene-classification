"""Thread-safe, globally rate-limited PubMed counts persisted across builds."""

import os
import sqlite3
import threading
import time
from urllib.parse import urlencode

from . import PARAMS, http


class Precheck:
    def __init__(
        self,
        cache,
        api_key=None,
        clock=time.monotonic,
        sleep=time.sleep,
        wall_clock=time.time,
        ttl_days=PARAMS["precheck_cache_ttl_days"],
    ):
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
        self.interval = 1 / (10 if self.api_key else 3) + 0.001
        self.clock, self.sleep, self.last = clock, sleep, None

    def throttle(self):
        # One shared limiter for every worker and every retry, without bursts.
        with self.lock:
            if self.last is not None:
                self.sleep(max(0, self.interval - (self.clock() - self.last)))
            self.last = self.clock()

    def count(self, gene, code, query):
        key = f"{gene}|{code}"
        with self.lock:
            cached = self.cache.get(key)
        # Query overrides can evolve: do not reuse an old phrase's count.
        term = f'"{gene}"[tiab] AND ({query})'
        if (
            cached is not None
            and cached["query"] == term
            and 0 <= self.wall_clock() - cached["fetched_at"] < self.ttl_seconds
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
