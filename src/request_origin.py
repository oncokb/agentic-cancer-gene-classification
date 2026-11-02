"""Same-origin checks for browser (cookie-authenticated) requests.

SameSite=Lax cookies still ride along on requests from a same-site sibling
origin, so state-changing endpoints a browser session can reach need proof
the request came from this app's own pages.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from src.config import settings

Origin = tuple[str, str, int]


def parse_origin(value: str, *, allow_path: bool = False) -> Optional[Origin]:
    """(scheme, host, port) of an http(s) origin; None for anything else.

    With allow_path, a path (but no query or fragment) may follow, as in a
    configured base URL."""
    try:
        url = urlsplit(value)
        if (url.scheme not in {"http", "https"} or not url.hostname or url.username or
                url.password or (not allow_path and url.path not in {"", "/"}) or
                url.query or url.fragment):
            return None
        return url.scheme, url.hostname.lower(), url.port or (443 if url.scheme == "https" else 80)
    except ValueError:
        return None


def public_origin() -> Optional[Origin]:
    """The origin of the configured PUBLIC_APP_BASE_URL (any path ignored)."""
    return parse_origin(settings.public_app_base_url, allow_path=True)


def _referer_origin(value: str) -> Optional[Origin]:
    try:
        url = urlsplit(value)
    except ValueError:
        return None
    return parse_origin(f"{url.scheme}://{url.netloc}") if url.netloc else None


def require_same_origin_json(request: Request) -> None:
    """For a cookie-authenticated write: 403 without same-origin evidence,
    415 unless the body is declared application/json.

    Evidence is `Sec-Fetch-Site: same-origin`; a browser that sends no
    Sec-Fetch-Site must send an Origin (or, failing that, a Referer) exactly
    matching PUBLIC_APP_BASE_URL's origin. JSON is required so a
    cross-origin "simple" request (text/plain, form, no Content-Type) can't
    reach the handler without a CORS preflight."""
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None:
        if fetch_site != "same-origin":
            raise HTTPException(403, "Cross-origin requests are forbidden.")
    else:
        expected = public_origin()
        origin = request.headers.get("origin")
        if origin is not None:
            actual = parse_origin(origin)
        else:
            referer = request.headers.get("referer")
            actual = _referer_origin(referer) if referer else None
        if expected is None or actual != expected:
            raise HTTPException(403, "Same-origin request evidence required.")
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(415, "Content-Type must be application/json.")
