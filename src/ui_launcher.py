"""Local desktop-style launcher for the browser UI."""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from typing import Optional

import uvicorn

from src.main import app


def find_available_port(host: str = "127.0.0.1") -> int:
    """Ask the OS for an available local TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def wait_for_server(url: str, timeout_seconds: float = 15.0) -> bool:
    """Wait until the local UI responds to HTTP requests."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                return response.status < 500
        except (OSError, urllib.error.URLError):
            time.sleep(0.2)
    return False


def open_when_ready(url: str) -> None:
    """Open the UI in the default browser once the local server is ready."""
    if wait_for_server(url):
        webbrowser.open(url)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Launch the Agentic Cancer Gene Classification browser UI."
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Local host interface to bind (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Local port to bind. Defaults to an available ephemeral port.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Start the local UI server without opening a browser window.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = parse_args(argv)
    port = args.port or find_available_port(args.host)
    url = f"http://{args.host}:{port}"

    if not args.no_browser:
        opener = threading.Thread(target=open_when_ready, args=(url,), daemon=True)
        opener.start()

    print(f"Opening annotation UI at {url}", file=sys.stderr)
    print("Leave this process running while using the UI.", file=sys.stderr)

    uvicorn.run(
        app,
        host=args.host,
        port=port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
