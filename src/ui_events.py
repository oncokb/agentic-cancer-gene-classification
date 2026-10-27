"""Browser engagement event intake. See docs/ui-events.md for the wire contract."""

from __future__ import annotations

import json
import logging
import re
import time
from collections import OrderedDict, deque
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Literal, Optional, Union
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import (
    BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError,
    field_validator, model_validator,
)

from src.auth import _uses_api_key, require_auth
from src.config import settings
from src.observability import increment, reset_user_context, set_user_context, stable_user_key
from src.pipeline.cache import _get_client

logger = logging.getLogger(__name__)
router = APIRouter()
_MAX_BODY = 32 * 1024
_SYMBOL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_ITEM = re.compile(r"[A-Za-z0-9_.:-]{1,32}\Z")
_FLAG_KEY = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}\Z")
_VARIANT = re.compile(r"[A-Za-z0-9_.-]{1,32}\Z")

EventType = Literal["surfaced", "impression", "open", "close", "click", "state", "action"]
Surface = Literal["results", "fusion_evidence", "no_result", "sidebar", "runs", "feedback", "global"]
Section = Literal[
    "gene_card", "rationale", "gene_summary", "lazy_field", "retrieved_pmids", "evidence_cards",
    "evidence_card", "supporting_quotes", "supporting_quote", "pmid_link",
    "clinical_actionability", "score_breakdown", "openevidence", "openevidence_content",
    "openevidence_guideline_link", "domains_treatments", "fusion_section", "fusion_card",
    "fusion_paper", "fusion_partner_check", "sidebar_entry", "sidebar_filter", "tab",
    "runs_page", "runs_row", "runs_pagination", "feedback_modal",
]
State = Literal[
    "loading", "pending", "ready", "cached", "unavailable", "failed", "timeout",
    "filter_used", "filter_cleared", "jump", "load_more", "retry", "submit", "submit_failed", "opened_run",
]


class Flag(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    variant: StrictStr = Field(max_length=32)
    version: StrictInt

    @field_validator("variant")
    @classmethod
    def safe_variant(cls, value: str) -> str:
        if not _VARIANT.fullmatch(value):
            raise ValueError("invalid flag variant")
        return value


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    event_id: UUID = Field(strict=False)
    ts: StrictInt
    type: EventType
    surface: Surface
    section: Section
    state: Optional[State] = None
    subject_kind: Literal["gene", "fusion", "run", "none"]
    subject_id: Optional[StrictStr] = Field(default=None, max_length=64)
    run_id: Optional[StrictStr] = Field(default=None, max_length=64)
    item_id: Optional[StrictStr] = Field(default=None, max_length=32)
    position: Optional[StrictInt] = Field(default=None, ge=0)
    display_position: Optional[StrictInt] = Field(default=None, ge=0)
    tumor_type_given: Optional[StrictBool] = None
    flags: Optional[dict[str, Flag]] = Field(default=None, max_length=10)

    @field_validator("run_id")
    @classmethod
    def valid_run_id(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not _uuid4_string(value):
            raise ValueError("run_id must be UUID4")
        return value

    @field_validator("item_id")
    @classmethod
    def valid_item_id(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not _ITEM.fullmatch(value):
            raise ValueError("invalid item_id")
        return value

    @field_validator("flags")
    @classmethod
    def safe_flag_keys(cls, value: Optional[dict[str, Flag]]) -> Optional[dict[str, Flag]]:
        if value is not None and any(not _FLAG_KEY.fullmatch(key) for key in value):
            raise ValueError("invalid flag key")
        return value

    @field_validator("event_id")
    @classmethod
    def uuid4_only(cls, value: UUID) -> UUID:
        if value.version != 4:
            raise ValueError("event_id must be UUID4")
        return value

    @model_validator(mode="after")
    def valid_state(self) -> Event:
        progress = {"loading", "pending", "ready", "cached", "unavailable", "failed", "timeout"}
        actions = {"filter_used", "filter_cleared", "jump", "load_more", "retry", "submit", "submit_failed", "opened_run"}
        if self.type == "state":
            if self.section != "openevidence" or self.state not in progress:
                raise ValueError("state events require an OpenEvidence progress state")
        elif self.type == "action":
            if self.state not in actions:
                raise ValueError("action events require an action state")
        elif self.state is not None:
            raise ValueError("state must be null for this event type")
        if self.subject_id is not None:
            if self.subject_kind == "gene":
                valid = bool(_SYMBOL.fullmatch(self.subject_id))
            elif self.subject_kind == "fusion":
                parts = self.subject_id.split("::")
                valid = len(parts) == 2 and all(_SYMBOL.fullmatch(part) for part in parts)
            elif self.subject_kind == "run":
                valid = _uuid4_string(self.subject_id)
            else:
                valid = False
            if not valid:
                raise ValueError("invalid subject_id for subject_kind")
        return self


def _uuid4_string(value: str) -> bool:
    try:
        parsed = UUID(value)
        return parsed.version == 4 and str(parsed) == value.lower()
    except ValueError:
        return False


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1]
    page_view_id: UUID = Field(strict=False)
    events: list[Any] = Field(min_length=1, max_length=50)

    @field_validator("page_view_id")
    @classmethod
    def uuid4_only(cls, value: UUID) -> UUID:
        if value.version != 4:
            raise ValueError("page_view_id must be UUID4")
        return value


_local_lock = Lock()
_local_rate: dict[str, deque[float]] = {}
_local_ids: OrderedDict[str, float] = OrderedDict()
_MAX_LOCAL_IDS = 10000
_last_redis_warning = 0.0


def _origin(value: str, *, allow_path: bool = False) -> Optional[tuple[str, str, int]]:
    try:
        url = urlsplit(value)
        if (url.scheme not in {"http", "https"} or not url.hostname or url.username or
                url.password or (not allow_path and url.path not in {"", "/"}) or
                url.query or url.fragment):
            return None
        return url.scheme, url.hostname.lower(), url.port or (443 if url.scheme == "https" else 80)
    except ValueError:
        return None


def _guard(request: Request) -> None:
    origin = request.headers.get("origin")
    public_origin = _origin(settings.public_app_base_url, allow_path=True)
    if origin is not None and (public_origin is None or _origin(origin) != public_origin):
        raise HTTPException(403, "Cross-origin UI events are forbidden")
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site != "same-origin":
        raise HTTPException(403, "Cross-origin UI events are forbidden")
    beacon = request.query_params.get("beacon") == "1"
    if beacon:
        if origin is None and fetch_site != "same-origin":
            raise HTTPException(403, "Beacon requires same-origin evidence")
    elif request.headers.get("x-acgc-events") != "1":
        raise HTTPException(403, "X-ACGC-Events header required")


def _redis_unavailable() -> None:
    global _last_redis_warning
    now = time.monotonic()
    with _local_lock:
        if now - _last_redis_warning >= 60:
            _last_redis_warning = now
            logger.warning("UI events Redis unavailable; dropping batch")


def _reject(code: int, detail: str, *, headers: Optional[dict[str, str]] = None) -> None:
    increment("ui.batches", tags=[f"outcome:{code}"])
    raise HTTPException(code, detail, headers=headers)


async def _charge_rate(user_key: str) -> bool:
    now = time.time()
    configured = bool(settings.redis_url.strip() or settings.redis_sentinel_enabled)
    if configured:
        try:
            redis = _get_client()
            window = int(now // 60)
            key = f"acgc:ui:rate:{user_key}:{window}"
            pipe = redis.pipeline()
            pipe.incr(key)
            pipe.expire(key, 65)
            count, _ = await pipe.execute()
            if count > settings.ui_events_rate_limit_per_minute:
                _reject(429, "UI events rate limit exceeded", headers={
                    "Retry-After": str(max(1, 60 - int(now) % 60)),
                })
            return True
        except HTTPException:
            raise
        except Exception:
            _redis_unavailable()
            return False

    with _local_lock:
        timestamps = _local_rate.setdefault(user_key, deque())
        while timestamps and timestamps[0] <= now - 60:
            timestamps.popleft()
        if len(timestamps) >= settings.ui_events_rate_limit_per_minute:
            _reject(429, "UI events rate limit exceeded", headers={
                "Retry-After": str(max(1, int(timestamps[0] + 60 - now) + 1)),
            })
        timestamps.append(now)
        return True


async def _dedupe(user_key: str, events: list[Event]) -> Optional[tuple[list[Event], int]]:
    if settings.redis_url.strip() or settings.redis_sentinel_enabled:
        try:
            redis = _get_client()
            # A transactional pipeline sends every SET NX in one round trip.
            # If execute loses its reply after Redis commits, IDs can remain
            # marked for ten minutes although this batch is dropped.
            pipe = redis.pipeline(transaction=True)
            for event in events:
                pipe.set(f"acgc:ui:event:{user_key}:{event.event_id}", "1", nx=True, ex=600)
            results = await pipe.execute()
            accepted = [event for event, inserted in zip(events, results) if inserted]
            return accepted, len(events) - len(accepted)
        except Exception:
            _redis_unavailable()
            return None

    now = time.time()
    with _local_lock:
        accepted = []
        duplicates = 0
        for event in events:
            key = f"{user_key}:{event.event_id}"
            if key in _local_ids and _local_ids[key] > now:
                duplicates += 1
                continue
            _local_ids[key] = now + 600
            _local_ids.move_to_end(key)
            accepted.append(event)
        while _local_ids and (len(_local_ids) > _MAX_LOCAL_IDS or next(iter(_local_ids.values())) <= now):
            _local_ids.popitem(last=False)
        return accepted, duplicates


def _metric_flags() -> dict[str, set[str]]:
    allowed = {}
    for entry in settings.ui_events_metric_flag_keys.split(","):
        key, separator, values = entry.strip().partition(":")
        if separator and _FLAG_KEY.fullmatch(key):
            variants = {value for value in values.split("|") if _VARIANT.fullmatch(value)}
            if variants:
                allowed[key] = variants
    return allowed


@router.post("/v1/events", status_code=202, response_model=None)
async def post_events(request: Request) -> Union[Response, dict[str, int]]:
    if not settings.ui_events_enabled:
        increment("ui.batches", tags=["outcome:204"])
        return Response(status_code=204)
    if _uses_api_key(request):
        _reject(403, "UI events require a browser session")
    try:
        user = await require_auth(request)
    except HTTPException as exc:
        increment("ui.batches", tags=[f"outcome:{exc.status_code}"])
        raise
    if user.provider == "api_key":
        _reject(403, "UI events require a browser session")
    try:
        _guard(request)
    except HTTPException:
        increment("ui.batches", tags=["outcome:403"])
        raise
    user_key = stable_user_key(user.email)
    if not await _charge_rate(user_key):
        increment("ui.batches", tags=["outcome:redis_unavailable"])
        return {"accepted": 0, "rejected": 0, "duplicates": 0}
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in {"application/json", "text/plain"}:
        _reject(422, "Expected JSON body")
    if content_type == "text/plain" and request.query_params.get("beacon") != "1":
        _reject(422, "text/plain is only accepted for beacons")
    length = request.headers.get("content-length")
    if length is not None:
        try:
            if int(length) > _MAX_BODY:
                _reject(413, "UI events body exceeds 32 KB")
        except ValueError:
            _reject(422, "Invalid Content-Length")
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > _MAX_BODY:
            _reject(413, "UI events body exceeds 32 KB")
        chunks.append(chunk)
    body = b"".join(chunks)
    try:
        envelope = Envelope.model_validate(json.loads(body))
    except Exception:
        _reject(422, "Invalid UI events envelope")
    now_ms = int(time.time() * 1000)
    valid = []
    rejected = 0
    for raw in envelope.events:
        try:
            event = Event.model_validate(raw)
        except ValidationError:
            increment("ui.events.rejected", tags=["reason:invalid_event"])
            rejected += 1
            continue
        if event.ts < now_ms - 24 * 3600 * 1000:
            reason = "too_old"
        elif event.ts > now_ms + 5 * 60 * 1000:
            reason = "future"
        else:
            valid.append(event)
            continue
        increment("ui.events.rejected", tags=[f"reason:{reason}"])
        rejected += 1
    result = await _dedupe(user_key, valid)
    if result is None:
        increment("ui.batches", tags=["outcome:redis_unavailable"])
        return {"accepted": 0, "rejected": rejected, "duplicates": 0}
    accepted, duplicates = result
    flag_keys = _metric_flags()
    for event in accepted:
        tags = [f"type:{event.type}", f"surface:{event.surface}", f"section:{event.section}"]
        if event.state is not None:
            tags.append(f"state:{event.state}")
        for key, flag in (event.flags or {}).items():
            if flag.variant in flag_keys.get(key, set()):
                tags.append(f"variant:{key}.{flag.variant}")
        increment("ui.events", tags=tags)
        payload = event.model_dump(mode="json")
        payload.update(page_view_id=str(envelope.page_view_id), user_key=user_key,
                       server_ts=datetime.now(timezone.utc).isoformat(), auth_provider=user.provider)
        token = set_user_context(user_id=user.email, auth_method=user.provider)
        try:
            logger.info("ui_event %s", json.dumps(payload, separators=(",", ":")))
        finally:
            reset_user_context(token)
    increment("ui.batches", tags=["outcome:202"])
    return {"accepted": len(accepted), "rejected": rejected, "duplicates": duplicates}
