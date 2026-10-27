"""Browser engagement event intake. See docs/ui-events.md for the wire contract."""

from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict, deque
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Literal, Optional, Union
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError, field_validator, model_validator

from src.auth import _uses_api_key, require_auth
from src.config import settings
from src.observability import increment, stable_user_key
from src.pipeline.cache import _get_client

logger = logging.getLogger(__name__)
router = APIRouter()

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
        return self


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


def _origin(value: str) -> tuple[str, str, int | None] | None:
    try:
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.path not in {"", "/"} or url.query or url.fragment:
            return None
        return url.scheme, url.hostname.lower(), url.port or (443 if url.scheme == "https" else 80)
    except ValueError:
        return None


def _guard(request: Request) -> None:
    origin = request.headers.get("origin")
    allowed = {_origin(str(request.base_url)), _origin(settings.public_app_base_url)}
    if origin is not None and _origin(origin) not in allowed:
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


async def _rate_and_dedupe(user_key: str, events: list[Event]) -> Optional[tuple[list[Event], int]]:
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
                raise HTTPException(429, "UI events rate limit exceeded", headers={"Retry-After": str(max(1, 60 - int(now) % 60))})
            accepted = []
            duplicates = 0
            for event in events:
                if await redis.set(f"acgc:ui:event:{user_key}:{event.event_id}", "1", nx=True, ex=600):
                    accepted.append(event)
                else:
                    duplicates += 1
            return accepted, duplicates
        except HTTPException:
            raise
        except Exception:
            logger.warning("UI events Redis unavailable; dropping batch")
            return None

    with _local_lock:
        timestamps = _local_rate.setdefault(user_key, deque())
        while timestamps and timestamps[0] <= now - 60:
            timestamps.popleft()
        if len(timestamps) >= settings.ui_events_rate_limit_per_minute:
            raise HTTPException(429, "UI events rate limit exceeded", headers={"Retry-After": str(max(1, int(timestamps[0] + 60 - now) + 1))})
        timestamps.append(now)
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


@router.post("/v1/events", status_code=202, response_model=None)
async def post_events(request: Request) -> Union[Response, dict[str, int]]:
    if not settings.ui_events_enabled:
        return Response(status_code=204)
    if _uses_api_key(request):
        raise HTTPException(403, "UI events require a browser session")
    user = await require_auth(request)
    if user.provider == "api_key":
        raise HTTPException(403, "UI events require a browser session")
    _guard(request)
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() not in {"application/json", "text/plain"}:
        raise HTTPException(422, "Expected JSON body")
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() == "text/plain" and request.query_params.get("beacon") != "1":
        raise HTTPException(422, "text/plain is only accepted for beacons")
    body = await request.body()
    if len(body) > 32 * 1024:
        increment("ui.batches", tags=["outcome:too_large"])
        raise HTTPException(413, "UI events body exceeds 32 KB")
    try:
        envelope = Envelope.model_validate(json.loads(body))
    except (ValueError, ValidationError, UnicodeDecodeError):
        increment("ui.batches", tags=["outcome:invalid"])
        raise HTTPException(422, "Invalid UI events envelope") from None
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
    user_key = stable_user_key(user.email)
    result = await _rate_and_dedupe(user_key, valid)
    if result is None:
        increment("ui.batches", tags=["outcome:redis_unavailable"])
        return {"accepted": 0, "rejected": rejected, "duplicates": 0}
    accepted, duplicates = result
    flag_keys = {key.strip() for key in settings.ui_events_metric_flag_keys.split(",") if key.strip()}
    for event in accepted:
        tags = [f"type:{event.type}", f"surface:{event.surface}", f"section:{event.section}"]
        if event.state is not None:
            tags.append(f"state:{event.state}")
        for key, flag in (event.flags or {}).items():
            if key in flag_keys and key.isidentifier() and flag.variant.isidentifier():
                tags.extend([f"exp:{key}", f"variant:{flag.variant}"])
        increment("ui.events", tags=tags)
        payload = event.model_dump(mode="json")
        payload.update(page_view_id=str(envelope.page_view_id), user_key=user_key,
                       server_ts=datetime.now(timezone.utc).isoformat(), auth_provider=user.provider)
        logger.info("ui_event %s", json.dumps(payload, separators=(",", ":")))
    increment("ui.batches", tags=["outcome:accepted"])
    return {"accepted": len(accepted), "rejected": rejected, "duplicates": duplicates}
