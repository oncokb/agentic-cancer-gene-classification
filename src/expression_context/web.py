"""The expression-context statistics page (mounted only when enabled).

- ``GET /expression-context``: the page (signed-out browsers are sent through
  ``/login`` and back, like ACGC's other pages).
- ``GET /expression-context/static/{asset}``: the page's own JS/CSS, served
  from ``src/expression_context/static/`` (an explicit allow-list).
- ``NavLinkMiddleware``: adds an "Expression stats" tab to the main page's
  ``<nav class="app-tabs">`` on the way out, so ACGC's own ``index.html`` and
  ``app.js`` need no edits and the entry disappears with the flag. The tab is
  only injected when ``nav_tab_enabled`` is true; otherwise the page is
  reachable by direct link only.

All of it is removed with the package; the core UI is untouched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, MutableMapping
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse, RedirectResponse, Response

from src.auth import get_request_user, session_store_unavailable, validate_redirect_to

PAGE_PATH = "/expression-context"
STATIC_PREFIX = "/expression-context/static"
STATIC_DIR = Path(__file__).parent / "static"
PAGE_FILE = "expression-context.html"
ASSETS: Dict[str, str] = {
    "expression-context.js": "text/javascript; charset=utf-8",
    "expression-context.css": "text/css; charset=utf-8",
}
NO_CACHE = {"Cache-Control": "no-cache"}

NAV_LINK_ID = "nav-expression-context"
NAV_LINK = (
    f'<a class="app-tab" id="{NAV_LINK_ID}" href="{PAGE_PATH}" '
    'style="text-decoration: none">Expression stats</a>'
)
# The tab goes right after ACGC's Benchmark tab button.
_NAV_ANCHOR = 'id="nav-benchmark"'
_MAX_INJECT_BYTES = 2 * 1024 * 1024

page_router = APIRouter(include_in_schema=False)


async def _login_redirect(request: Request) -> Response | None:
    from src.config import settings as acgc_settings

    if not acgc_settings.auth_enabled:
        return None
    user = await get_request_user(request)
    if session_store_unavailable(request):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session verification is temporarily unavailable. Please retry shortly.",
        )
    if user:
        return None
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(
        url=f"/login?redirect_to={quote(validate_redirect_to(target), safe='')}",
        status_code=303,
    )


@page_router.get(PAGE_PATH)
async def expression_context_page(request: Request) -> Response:
    redirect = await _login_redirect(request)
    if redirect:
        return redirect
    return FileResponse(STATIC_DIR / PAGE_FILE, media_type="text/html", headers=NO_CACHE)


@page_router.get(STATIC_PREFIX + "/{asset}")
async def expression_context_asset(asset: str) -> Response:
    media_type = ASSETS.get(asset)
    if media_type is None:
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(STATIC_DIR / asset, media_type=media_type, headers=NO_CACHE)


def inject_nav_link(html: bytes) -> bytes:
    """Insert the nav tab after ACGC's Benchmark tab; unchanged if not found."""
    try:
        text = html.decode("utf-8")
    except UnicodeDecodeError:
        return html
    if f'id="{NAV_LINK_ID}"' in text:
        return html
    anchor = text.find(_NAV_ANCHOR)
    if anchor < 0:
        return html
    end = text.find("</button>", anchor)
    if end < 0:
        return html
    end += len("</button>")
    return (text[:end] + "\n          " + NAV_LINK + text[end:]).encode("utf-8")


Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]


class NavLinkMiddleware:
    """Pure-ASGI middleware that rewrites only ``GET /`` HTML responses."""

    def __init__(self, app: Callable[..., Awaitable[None]], nav_tab_enabled: bool = True) -> None:
        self.app = app
        self.nav_tab_enabled = nav_tab_enabled

    async def __call__(self, scope: Scope, receive: Callable, send: Callable) -> None:
        if (
            not self.nav_tab_enabled
            or scope.get("type") != "http"
            or scope.get("method") != "GET"
            or scope.get("path") != "/"
        ):
            await self.app(scope, receive, send)
            return

        start: Dict[str, Any] = {}
        body = bytearray()
        passthrough = False

        async def flush_unmodified(message: Message) -> None:
            nonlocal passthrough
            passthrough = True
            await send(start["message"])
            await send({"type": "http.response.body", "body": bytes(body),
                        "more_body": message.get("more_body", False)})

        async def wrapped_send(message: Message) -> None:
            nonlocal passthrough
            if passthrough:
                await send(message)
                return
            if message["type"] == "http.response.start":
                headers = {k.lower(): v for k, v in message.get("headers", [])}
                content_type = headers.get(b"content-type", b"").decode("latin-1")
                if (
                    message.get("status") != 200
                    or not content_type.startswith("text/html")
                    or b"content-encoding" in headers
                ):
                    passthrough = True
                    await send(message)
                    return
                start["message"] = message
                return
            if message["type"] != "http.response.body" or "message" not in start:
                await send(message)
                return
            body.extend(message.get("body", b""))
            if len(body) > _MAX_INJECT_BYTES:
                await flush_unmodified(message)
                return
            if message.get("more_body", False):
                return
            new_body = inject_nav_link(bytes(body))
            response_start = start["message"]
            headers = [
                (key, value)
                for key, value in response_start.get("headers", [])
                if key.lower() not in (b"content-length", b"etag", b"last-modified")
            ]
            headers.append((b"content-length", str(len(new_body)).encode("latin-1")))
            await send({**response_start, "headers": headers})
            await send({"type": "http.response.body", "body": new_body, "more_body": False})

        await self.app(scope, receive, wrapped_send)
