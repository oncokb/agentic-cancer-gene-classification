"""Settings for the optional expression-context module.

Deliberately separate from ``src/config.py``: every knob is read from an
``EXPRESSION_CONTEXT_*`` environment variable so the whole feature can be
removed without touching the core settings class.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ExpressionContextSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EXPRESSION_CONTEXT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Off by default: no routes, no table, no outbound calls, no startup work.
    enabled: bool = False

    # Base URL of the stats repo's published artifacts (the v1 JSON contract
    # lives under {artifact_base_url}/v1/...). Required when enabled.
    artifact_base_url: str = ""

    http_timeout_seconds: float = Field(default=10.0, gt=0)
    http_connect_timeout_seconds: float = Field(default=5.0, gt=0)
    http_retry_attempts: int = Field(default=3, ge=1, le=10)
    # Multiplier for tenacity's exponential back-off between retries.
    http_retry_backoff_seconds: float = Field(default=1.0, ge=0)
    http_retry_backoff_max_seconds: float = Field(default=8.0, ge=0)

    # Redis cached_call TTLs for the artifact files. Gene and alias files are
    # keyed by release_id, so they can live longer than the manifest.
    manifest_cache_ttl_seconds: int = Field(default=3600, ge=1)
    gene_cache_ttl_seconds: int = Field(default=86400, ge=1)

    # Literature gate implementation (the stored verdict key includes it).
    literature_gate: Literal["acgc_pipeline_v1"] = "acgc_pipeline_v1"
    # Max literature-gate evaluations running at once within one job.
    literature_concurrency: int = Field(default=2, ge=1, le=16)
    # Annotation mode for the ACGC pipeline gate.
    pipeline_mode: Literal["full", "core"] = "full"

    # The module's own small MySQL pool for expression_context_results.
    mysql_pool_maxsize: int = Field(default=2, ge=1, le=20)

    history_max_limit: int = Field(default=100, ge=1)

    @model_validator(mode="after")
    def _require_base_url_when_enabled(self) -> "ExpressionContextSettings":
        if self.enabled and not self.artifact_base_url.strip():
            raise ValueError(
                "EXPRESSION_CONTEXT_ARTIFACT_BASE_URL is required when "
                "EXPRESSION_CONTEXT_ENABLED=true"
            )
        return self

    @property
    def base_url(self) -> str:
        return self.artifact_base_url.strip().rstrip("/")
