"""Small injectable HTTP client with bounded retries and atomic downloads."""

import json
import time
from http.client import IncompleteRead
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class InvalidJSONResponse(ValueError):
    """A JSON response failed validation and can be retried."""


def _check_remaining(remaining):
    if remaining is None:
        return 120
    rem = remaining()
    if rem <= 0:
        raise TimeoutError("Remaining time budget exhausted")
    return min(120, rem)


def request(url, *, data=None, headers=None, before=None, attempts=4, remaining=None, read=None):
    for attempt in range(attempts):
        if before:
            before()
        req_timeout = _check_remaining(remaining)
        try:
            response = urlopen(
                Request(
                    url,
                    data=data,
                    headers={"User-Agent": "gene-expression-relevance/0.1", **(headers or {})},
                ),
                timeout=req_timeout,
            )
            if read:
                with response:
                    return read(response)
            return response
        except (
            HTTPError,
            URLError,
            TimeoutError,
            InvalidJSONResponse,
            json.JSONDecodeError,
        ) as exc:
            if isinstance(exc, HTTPError) and exc.code not in (408, 429, 500, 502, 503, 504):
                raise
            rem_after = _check_remaining(remaining) if remaining else None
            if attempt == attempts - 1:
                raise
            delay = 2**attempt
            if isinstance(exc, HTTPError):
                try:
                    delay = max(delay, float(exc.headers.get("Retry-After", 0)))
                except ValueError:
                    pass
            time.sleep(min(delay, rem_after) if rem_after is not None else delay)
            if remaining:
                remaining()


def get_json(url, *, validate=None, **kwargs):
    def read(response):
        result = json.load(response)
        if validate:
            validate(result)
        return result

    return request(url, read=read, **kwargs)


def download(url, target, *, headers=None, remaining=None):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".part")
    try:
        for attempt in range(4):
            if remaining:
                remaining()
            try:
                with (
                    request(url, headers=headers, remaining=remaining) as response,
                    temporary.open("wb") as output,
                ):
                    while block := response.read(1024 * 1024):
                        if remaining:
                            remaining()
                        output.write(block)
                temporary.replace(target)
                return
            except (IncompleteRead, ConnectionError, TimeoutError):
                rem_after = _check_remaining(remaining) if remaining else None
                if attempt == 3:
                    raise
                time.sleep(min(2**attempt, rem_after) if rem_after is not None else 2**attempt)
                if remaining:
                    remaining()
    finally:
        temporary.unlink(missing_ok=True)
