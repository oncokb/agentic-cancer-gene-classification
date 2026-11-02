"""AGCG_DEV_MODE (every signed-in user is admin, dev login on) refuses to start
when PUBLIC_APP_BASE_URL names a non-local host; local development still works."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from src import main as main_module
from src.config import Settings, dev_mode_startup_error
from src.main import app


def _settings(**values) -> Settings:
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://acgc.oncokb.org",
        "http://acgc.oncokb.org:8000",
        "acgc.oncokb.org",
        "https://localhost.evil.example",
        "https://10.0.0.5",
        "http://[::2]:8000",
        "http://:8000",  # no host to check
    ],
)
def test_dev_mode_with_a_non_local_base_url_refuses_to_start(base_url):
    error = dev_mode_startup_error(_settings(agcg_dev_mode=True, public_app_base_url=base_url))
    assert error is not None
    assert "AGCG_DEV_MODE" in error and "Refusing to start" in error


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:8000",
        "https://LOCALHOST",
        "localhost:8000",
        "http://127.0.0.1:8000",
        "http://[::1]:8000",
        "http://0.0.0.0",
    ],
)
def test_dev_mode_with_a_local_base_url_starts(base_url):
    assert dev_mode_startup_error(_settings(agcg_dev_mode=True, public_app_base_url=base_url)) is None


@pytest.mark.parametrize("base_url", ["", "   ", "\t\n"])
def test_dev_mode_with_an_explicitly_blank_base_url_refuses_to_start(base_url):
    # Blank makes the app take its public URL from each request's host.
    error = dev_mode_startup_error(_settings(agcg_dev_mode=True, public_app_base_url=base_url))
    assert error is not None and "set but blank" in error


@pytest.mark.parametrize("base_url", ["", "  "])
def test_blank_base_url_without_dev_mode_starts(base_url):
    assert dev_mode_startup_error(_settings(public_app_base_url=base_url)) is None


def test_dev_mode_with_base_url_unset_starts():
    # Unset means the built-in default (the production URL) applies, which
    # isn't an operator pointing a dev-mode instance at production.
    config = _settings(agcg_dev_mode=True)
    assert config.public_app_base_url == "https://acgc.oncokb.org"
    assert dev_mode_startup_error(config) is None


def test_base_url_from_the_environment_counts_as_set(monkeypatch):
    monkeypatch.setenv("AGCG_DEV_MODE", "true")
    monkeypatch.setenv("PUBLIC_APP_BASE_URL", "https://acgc.oncokb.org")
    assert dev_mode_startup_error(Settings(_env_file=None)) is not None
    monkeypatch.setenv("PUBLIC_APP_BASE_URL", "http://localhost:8000")
    assert dev_mode_startup_error(Settings(_env_file=None)) is None
    for blank in ("", "   "):
        monkeypatch.setenv("PUBLIC_APP_BASE_URL", blank)
        assert dev_mode_startup_error(Settings(_env_file=None)) is not None
    monkeypatch.delenv("PUBLIC_APP_BASE_URL")
    assert dev_mode_startup_error(Settings(_env_file=None)) is None  # genuinely absent


def test_production_settings_start():
    # Production: PUBLIC_APP_BASE_URL set, AGCG_DEV_MODE not.
    assert dev_mode_startup_error(_settings(public_app_base_url="https://acgc.oncokb.org")) is None


class _NullRunStore:
    async def close(self) -> None:
        pass


@pytest.fixture
def lifespan_settings(monkeypatch):
    """Start the app's lifespan with a freshly built Settings, so whether
    PUBLIC_APP_BASE_URL was configured is captured as at real startup."""
    monkeypatch.setattr(main_module, "RunStore", SimpleNamespace(create=AsyncMock(return_value=_NullRunStore())))

    def configure(**values):
        monkeypatch.setattr(main_module, "settings", _settings(**values))

    return configure


@pytest.mark.parametrize("base_url", ["https://acgc.oncokb.org", "", "   "])
def test_app_refuses_to_start_in_dev_mode_against_a_production_host(lifespan_settings, base_url):
    lifespan_settings(agcg_dev_mode=True, public_app_base_url=base_url)
    with pytest.raises(RuntimeError, match="AGCG_DEV_MODE"):
        with TestClient(app):
            pass
    main_module.RunStore.create.assert_not_awaited()  # nothing else started


@pytest.mark.parametrize(
    "values",
    [
        {"agcg_dev_mode": True, "public_app_base_url": "http://localhost:8000"},
        {"agcg_dev_mode": True},  # PUBLIC_APP_BASE_URL unset
        {"public_app_base_url": "https://acgc.oncokb.org"},  # production
    ],
)
def test_app_starts_for_local_dev_and_production(lifespan_settings, values):
    lifespan_settings(**values)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
