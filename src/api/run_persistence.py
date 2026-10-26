"""Strict persistence for API responses that include a link to a saved run."""

import logging
from typing import Dict, Optional

from fastapi import HTTPException, Request

from src.auth import AuthenticatedUser
from src.config import settings
from src.models.schema import AnnotationResult

logger = logging.getLogger(__name__)

GENERIC_FAILURE = "Gene query failed. Please retry; contact the ACGC team if this persists."

# With auth disabled (local dev), require_auth signs every request in as this user.
LOCAL_USER_EMAIL = "local@internal"


def run_owner(current_user: Optional[AuthenticatedUser]) -> Dict[str, Optional[str]]:
    """The `owner_email`/`source` a run started by `current_user` is saved with.

    source is "api_key" for API-key requests, "local" with auth disabled, and
    otherwise "session" (a signed-in browser or session bearer token).
    """
    if not settings.auth_enabled:
        return {"owner_email": LOCAL_USER_EMAIL, "source": "local"}
    if current_user is None or not current_user.email:
        return {"owner_email": None, "source": None}
    source = "api_key" if current_user.auth_method == "api_key" else "session"
    return {"owner_email": current_user.email.strip().lower(), "source": source}


async def save_run_or_raise(
    http_request: Request,
    request_payload: dict,
    result: AnnotationResult,
    current_user: Optional[AuthenticatedUser],
) -> None:
    """Keep run links out of responses when their run cannot be persisted."""
    try:
        await http_request.app.state.run_store.save_run(
            result.run_id, result.timestamp, request_payload, result.model_dump(), **run_owner(current_user)
        )
    except Exception as exc:
        logger.exception("Failed to save linked run %s", result.run_id)
        raise HTTPException(status_code=500, detail=GENERIC_FAILURE) from exc
