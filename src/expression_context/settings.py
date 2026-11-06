"""Settings for the optional expression-context module.

Deliberately separate from ``src/config.py``: every knob is read from an
``EXPRESSION_CONTEXT_*`` environment variable so the whole feature can be
removed without touching the core settings class.

Statistics arrive as GitHub release bundles of ``EXPRESSION_CONTEXT_RELEASES_REPO``
(public), so turning the flag on needs no URL or credentials.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Literal, Optional

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BUNDLE_TAG_PREFIX = "expression-stats-"

# Worst-case GitHub API calls per hour the whole cluster may make (discovery
# is coordinated cluster-wide). GitHub allows 60/h per IP unauthenticated and
# 5000/h with a token; stay well under either.
UNAUTHENTICATED_API_BUDGET_PER_HOUR = 30
AUTHENTICATED_API_BUDGET_PER_HOUR = 1000

_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_TAG = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")


class ExpressionContextSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EXPRESSION_CONTEXT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Off by default: no routes, no tables, no outbound calls, no startup work.
    enabled: bool = False

    # ---------------------------------------------------------------- bundles
    # GitHub repository whose releases carry the statistics bundles
    # (tags expression-stats-<release_id>-pubmed-<YYYYMMDD>).
    releases_repo: str = "oncokb/agentic-cancer-gene-classification"
    # Load exactly this bundle tag and never advance past it (rollback/pinning).
    pinned_bundle_tag: Optional[str] = None
    # Optional; the repository is public. Only raises the GitHub API rate
    # limit (60/h per IP unauthenticated). Sent to the GitHub API host only.
    github_token: Optional[SecretStr] = None
    github_api_url: str = "https://api.github.com"
    # How often the cluster checks GitHub for a newer bundle. Coordinated
    # through MySQL, so it is ~one discovery per interval for all pods
    # together. Seconds or ISO 8601 (e.g. "PT6H").
    refresh_interval: timedelta = Field(default=timedelta(hours=6), ge=timedelta(minutes=5))
    # Back-off before retrying after a failed GitHub call (a rate-limit
    # response uses GitHub's own reset time instead, if later). Also the
    # re-check cadence while no bundle is loaded yet.
    refresh_error_retry: timedelta = Field(
        default=timedelta(minutes=30), ge=timedelta(minutes=1)
    )
    # Release-list pages (100 each) scanned per discovery. Every page is read
    # (the list is ordered by creation, the newest bundle is chosen by
    # published_at); if more pages remain, nothing is activated.
    max_release_pages: int = Field(default=5, ge=1, le=20)
    # A bundle that stops being active/previous is deleted only after this
    # long, so pods with a cached pointer and in-flight requests never lose it.
    bundle_prune_grace: timedelta = Field(default=timedelta(hours=1))

    http_timeout_seconds: float = Field(default=30.0, gt=0)
    http_connect_timeout_seconds: float = Field(default=10.0, gt=0)
    http_retry_attempts: int = Field(default=3, ge=1, le=10)
    # Multiplier for tenacity's exponential back-off between retries.
    http_retry_backoff_seconds: float = Field(default=1.0, ge=0)
    http_retry_backoff_max_seconds: float = Field(default=8.0, ge=0)
    # Wall-clock limit for downloading and importing one genes.jsonl.gz.
    download_timeout_seconds: float = Field(default=1800.0, gt=0)

    # Size guards (bytes). The bundle's declared size is checked against
    # max_bundle_bytes before downloading anything large.
    max_bundle_bytes: int = Field(default=1024 * 1024 * 1024, ge=1)
    max_manifest_bytes: int = Field(default=5 * 1024 * 1024, ge=1)
    max_aliases_bytes: int = Field(default=64 * 1024 * 1024, ge=1)
    max_line_bytes: int = Field(default=8 * 1024 * 1024, ge=1024)
    # Genes per batched INSERT while importing.
    load_batch_size: int = Field(default=500, ge=1, le=5000)
    # Cross-pod single-loader lease (renewed while a bundle loads).
    bundle_lock_ttl_seconds: float = Field(default=120.0, gt=0)
    # How long a pod trusts its cached view of the active bundle pointer.
    active_bundle_cache_seconds: float = Field(default=30.0, ge=0)

    # ---------------------------------------------------------- module Redis
    # Redis for the module's leases. Empty = ACGC's REDIS_URL /
    # REDIS_SENTINEL_* settings (read, never modified).
    redis_url: str = ""

    # ------------------------------------------------------- literature gate
    # Literature gate implementation (the stored verdict key includes it).
    literature_gate: Literal["acgc_pipeline_v1"] = "acgc_pipeline_v1"
    # Max literature-gate evaluations running at once in this process,
    # shared across all jobs.
    literature_concurrency: int = Field(default=2, ge=1, le=16)
    # Cross-pod single-flight lease on a verdict key (renewed while the gate
    # runs); non-holders poll the store for the holder's verdict and fall
    # back to evaluating themselves after lease_wait_seconds.
    lease_ttl_seconds: float = Field(default=120.0, gt=0)
    lease_wait_seconds: float = Field(default=600.0, ge=0)
    lease_poll_seconds: float = Field(default=2.0, gt=0)
    # Annotation mode for the ACGC pipeline gate.
    pipeline_mode: Literal["full", "core"] = "full"

    # The module's own small MySQL pool (results + bundle tables). The
    # loader holds up to two connections while importing.
    mysql_pool_maxsize: int = Field(default=4, ge=2, le=20)

    history_max_limit: int = Field(default=100, ge=1)

    @field_validator("releases_repo")
    @classmethod
    def _check_repo(cls, value: str) -> str:
        value = value.strip()
        if not _REPO.fullmatch(value):
            raise ValueError("EXPRESSION_CONTEXT_RELEASES_REPO must look like 'owner/name'")
        return value

    @field_validator("pinned_bundle_tag")
    @classmethod
    def _check_pinned(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if not value.startswith(BUNDLE_TAG_PREFIX) or not _TAG.fullmatch(value):
            raise ValueError(
                f"EXPRESSION_CONTEXT_PINNED_BUNDLE_TAG must be a '{BUNDLE_TAG_PREFIX}…' release tag"
            )
        return value

    @field_validator("github_token")
    @classmethod
    def _blank_token_is_none(cls, value: Optional[SecretStr]) -> Optional[SecretStr]:
        if value is None or not value.get_secret_value().strip():
            return None
        return SecretStr(value.get_secret_value().strip())

    @field_validator("github_api_url")
    @classmethod
    def _check_api_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith("https://"):
            raise ValueError("EXPRESSION_CONTEXT_GITHUB_API_URL must be an https:// URL")
        return value

    @property
    def github_requests_per_hour_worst_case(self) -> float:
        """Upper bound on cluster-wide GitHub API calls per hour: discovery at
        the fastest cadence (errors / nothing loaded yet), every page, every
        retry attempt."""
        fastest = min(self.refresh_interval_seconds, self.refresh_error_retry_seconds)
        return 3600.0 / fastest * self.max_release_pages * self.http_retry_attempts

    @model_validator(mode="after")
    def _check_budget_and_grace(self) -> "ExpressionContextSettings":
        budget = (
            AUTHENTICATED_API_BUDGET_PER_HOUR
            if self.github_token is not None
            else UNAUTHENTICATED_API_BUDGET_PER_HOUR
        )
        worst = self.github_requests_per_hour_worst_case
        if worst > budget:
            raise ValueError(
                f"GitHub API budget exceeded: up to {worst:.0f} requests/hour "
                f"(3600 / min(REFRESH_INTERVAL, REFRESH_ERROR_RETRY) x MAX_RELEASE_PAGES "
                f"{self.max_release_pages} x HTTP_RETRY_ATTEMPTS {self.http_retry_attempts}) "
                f"> {budget}/hour allowed "
                f"{'with' if self.github_token is not None else 'without'} "
                "EXPRESSION_CONTEXT_GITHUB_TOKEN. Lengthen the intervals or lower the "
                "page cap / retries."
            )
        minimum = self.active_bundle_cache_seconds + 300
        if self.bundle_prune_grace.total_seconds() < minimum:
            raise ValueError(
                "EXPRESSION_CONTEXT_BUNDLE_PRUNE_GRACE must be at least "
                f"ACTIVE_BUNDLE_CACHE_SECONDS + 5 minutes ({minimum:.0f}s)"
            )
        return self

    @property
    def bundle_prune_grace_seconds(self) -> float:
        return self.bundle_prune_grace.total_seconds()

    @property
    def refresh_interval_seconds(self) -> float:
        return self.refresh_interval.total_seconds()

    @property
    def refresh_error_retry_seconds(self) -> float:
        return self.refresh_error_retry.total_seconds()
