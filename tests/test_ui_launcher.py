"""Tests for the local browser UI launcher."""

from __future__ import annotations

from src.ui_launcher import find_available_port, parse_args, wait_for_server


class _FakeSocket:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None

    def bind(self, address):
        self.address = address

    def getsockname(self):
        return ("127.0.0.1", 49321)


class _FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None


def test_find_available_port_asks_os_for_ephemeral_port(monkeypatch):
    monkeypatch.setattr("src.ui_launcher.socket.socket", lambda *args: _FakeSocket())

    assert find_available_port() == 49321


def test_wait_for_server_returns_true_when_endpoint_responds(monkeypatch):
    monkeypatch.setattr(
        "src.ui_launcher.urllib.request.urlopen",
        lambda url, timeout: _FakeResponse(),
    )

    assert wait_for_server("http://127.0.0.1:49321", timeout_seconds=2.0)


def test_parse_args_accepts_no_browser_and_port():
    args = parse_args(["--host", "127.0.0.1", "--port", "9000", "--no-browser"])

    assert args.host == "127.0.0.1"
    assert args.port == 9000
    assert args.no_browser is True
