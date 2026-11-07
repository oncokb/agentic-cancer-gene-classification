"""Small injectable HTTP client with bounded retries and atomic downloads."""

import json
import time
from http.client import IncompleteRead
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def request(url, *, data=None, headers=None, before=None, attempts=4, remaining=None):
    for attempt in range(attempts):
        if before:
            before()
        try:
            return urlopen(
                Request(
                    url,
                    data=data,
                    headers={"User-Agent": "gene-expression-relevance/0.1", **(headers or {})},
                ),
                timeout=min(120, remaining()) if remaining else 120,
            )
        except (HTTPError, URLError, TimeoutError) as exc:
            if isinstance(exc, HTTPError) and exc.code not in (408, 429, 500, 502, 503, 504):
                raise
            if attempt == attempts - 1:
                raise
            delay = 2**attempt
            if isinstance(exc, HTTPError):
                try:
                    delay = max(delay, float(exc.headers.get("Retry-After", 0)))
                except ValueError:
                    pass
            time.sleep(min(delay, remaining()) if remaining else delay)
            if remaining:
                remaining()


def get_json(url, **kwargs):
    with request(url, **kwargs) as response:
        return json.load(response)


def download(url, target, *, headers=None):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".part")
    try:
        for attempt in range(4):
            try:
                with request(url, headers=headers) as response, temporary.open("wb") as output:
                    while block := response.read(1024 * 1024):
                        output.write(block)
                temporary.replace(target)
                return
            except (IncompleteRead, ConnectionError, TimeoutError):
                if attempt == 3:
                    raise
                time.sleep(2**attempt)
    finally:
        temporary.unlink(missing_ok=True)
