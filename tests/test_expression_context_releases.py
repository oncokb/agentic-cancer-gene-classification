"""GitHub Releases client: bundle discovery and asset downloads (pytest-httpx;
no live network)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import ValidationError

from src.expression_context.github_releases import (
    DiscoveryIncomplete,
    RateLimited,
    ReleaseAsset,
    ReleasesClient,
    ReleasesError,
    parse_release,
    rate_limit_retry_at,
)
from src.expression_context.settings import ExpressionContextSettings
from tests.expression_context_helpers import (
    APP_RELEASE,
    GITHUB_API,
    LIST_URL,
    REPO,
    add_assets,
    asset_url,
    build_bundle,
    make_settings,
    release_json,
)


@pytest.fixture
async def client():
    releases = ReleasesClient(make_settings())
    yield releases
    await releases.aclose()


def _bundle(day: int, published: str):
    from datetime import date

    return build_bundle(pubmed=date(2026, 10, day), published_at=published)


# ------------------------------------------------------------------ settings


def test_settings_defaults_need_no_url_or_token(monkeypatch):
    for name in ("ENABLED", "RELEASES_REPO", "PINNED_BUNDLE_TAG", "GITHUB_TOKEN", "REFRESH_INTERVAL"):
        monkeypatch.delenv(f"EXPRESSION_CONTEXT_{name}", raising=False)
    settings = ExpressionContextSettings(_env_file=None, enabled=True)
    assert settings.releases_repo == REPO
    assert settings.pinned_bundle_tag is None and settings.github_token is None
    assert settings.refresh_interval_seconds == 6 * 3600
    assert not hasattr(settings, "artifact_base_url")


def test_settings_read_env(monkeypatch):
    monkeypatch.setenv("EXPRESSION_CONTEXT_ENABLED", "true")
    monkeypatch.setenv("EXPRESSION_CONTEXT_RELEASES_REPO", "someone/fork")
    monkeypatch.setenv("EXPRESSION_CONTEXT_PINNED_BUNDLE_TAG", "expression-stats-x-pubmed-20261101")
    monkeypatch.setenv("EXPRESSION_CONTEXT_GITHUB_TOKEN", " ghp_secret ")
    monkeypatch.setenv("EXPRESSION_CONTEXT_REFRESH_INTERVAL", "PT2H")
    settings = ExpressionContextSettings(_env_file=None)
    assert settings.releases_repo == "someone/fork"
    assert settings.pinned_bundle_tag == "expression-stats-x-pubmed-20261101"
    assert settings.github_token.get_secret_value() == "ghp_secret"
    assert "ghp_secret" not in repr(settings)
    assert settings.refresh_interval_seconds == 7200


@pytest.mark.parametrize(
    "field,value",
    [
        ("releases_repo", "not a repo"),
        ("pinned_bundle_tag", "v0.4.5"),
        ("github_api_url", "http://api.github.com"),
        ("refresh_interval", 60),  # below the 5-minute floor (protects the rate limit)
    ],
)
def test_settings_reject_bad_values(field, value):
    with pytest.raises(ValidationError):
        ExpressionContextSettings(_env_file=None, **{field: value})


def test_blank_optional_settings_mean_unset():
    settings = ExpressionContextSettings(_env_file=None, pinned_bundle_tag=" ", github_token="")
    assert settings.pinned_bundle_tag is None and settings.github_token is None


# ----------------------------------------------------------------- discovery


async def test_latest_bundle_filters_and_picks_newest_published(httpx_mock, client):
    old, new = _bundle(1, "2026-10-01T00:00:00Z"), _bundle(8, "2026-10-08T00:00:00Z")
    draft, pre = _bundle(15, "2026-10-15T00:00:00Z"), _bundle(22, "2026-10-22T00:00:00Z")
    httpx_mock.add_response(url=LIST_URL, json=[
        APP_RELEASE,                               # not a bundle tag
        release_json(old),                         # listed first but older
        release_json(draft, draft=True),           # drafts are never bundles
        release_json(pre, prerelease=True),        # nor prereleases
        release_json(new),
    ])
    release = await client.latest_bundle()
    assert release.tag == new.tag
    assert release.published_at == datetime(2026, 10, 8, tzinfo=timezone.utc)
    assert set(release.assets) == {"manifest.json", "genes.jsonl.gz", "aliases.json"}
    assert release.assets["genes.jsonl.gz"].download_url == asset_url(new.tag, "genes.jsonl.gz")
    assert release.html_url.startswith(f"https://github.com/{REPO}/releases/tag/")


async def test_latest_bundle_none_when_repo_has_no_bundles(httpx_mock, client):
    httpx_mock.add_response(url=LIST_URL, json=[APP_RELEASE])
    assert await client.latest_bundle() is None


async def test_latest_bundle_pages_until_a_bundle_appears(httpx_mock, client):
    page2 = f"{GITHUB_API}/repositories/1/releases?per_page=100&page=2"
    httpx_mock.add_response(
        url=LIST_URL,
        json=[APP_RELEASE] * 100,
        headers={"Link": f'<{page2}>; rel="next", <{page2}>; rel="last"'},
    )
    bundle = _bundle(8, "2026-10-08T00:00:00Z")
    httpx_mock.add_response(url=page2, json=[release_json(bundle)])
    assert (await client.latest_bundle()).tag == bundle.tag
    assert len(httpx_mock.get_requests()) == 2


async def test_latest_bundle_raises_when_the_page_cap_cuts_the_list(httpx_mock):
    client = ReleasesClient(make_settings(max_release_pages=2))
    pages = [f"{GITHUB_API}/repositories/1/releases?per_page=100&page={n}" for n in (2, 3)]
    httpx_mock.add_response(url=LIST_URL, json=[APP_RELEASE] * 100,
                            headers={"Link": f'<{pages[0]}>; rel="next"'})
    httpx_mock.add_response(url=pages[0], json=[APP_RELEASE] * 100,
                            headers={"Link": f'<{pages[1]}>; rel="next"'})
    try:
        with pytest.raises(DiscoveryIncomplete, match="discovery incomplete"):
            await client.latest_bundle()
        assert len(httpx_mock.get_requests()) == 2
    finally:
        await client.aclose()


async def test_bundle_by_tag(httpx_mock, client):
    bundle = _bundle(8, "2026-10-08T00:00:00Z")
    httpx_mock.add_response(
        url=f"{GITHUB_API}/repos/{REPO}/releases/tags/{bundle.tag}", json=release_json(bundle)
    )
    httpx_mock.add_response(
        url=f"{GITHUB_API}/repos/{REPO}/releases/tags/expression-stats-missing", status_code=404
    )
    assert (await client.bundle_by_tag(bundle.tag)).tag == bundle.tag
    assert await client.bundle_by_tag("expression-stats-missing") is None


def test_parse_release_ignores_non_https_assets():
    bundle = _bundle(8, "2026-10-08T00:00:00Z")
    payload = release_json(bundle)
    payload["assets"][0]["browser_download_url"] = "http://evil.example/manifest.json"
    assert "manifest.json" not in parse_release(payload).assets


@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
async def test_api_headers_and_token_only_to_api_host(httpx_mock):
    client = ReleasesClient(make_settings(github_token="ghp_secret"))
    bundle = _bundle(8, "2026-10-08T00:00:00Z")
    httpx_mock.add_response(url=LIST_URL, json=[release_json(bundle)])
    add_assets(httpx_mock, bundle)
    try:
        release = await client.latest_bundle()
        await client.fetch_json_asset(release.assets["manifest.json"], max_bytes=10_000)
    finally:
        await client.aclose()
    api_request, download = httpx_mock.get_requests()
    assert api_request.headers["authorization"] == "Bearer ghp_secret"
    assert api_request.headers["accept"] == "application/vnd.github+json"
    assert api_request.headers["x-github-api-version"] == "2022-11-28"
    assert api_request.headers["user-agent"] == "acgc-expression-context"
    assert "authorization" not in download.headers  # never sent to download/CDN hosts


async def test_no_token_means_no_authorization_header(httpx_mock, client):
    httpx_mock.add_response(url=LIST_URL, json=[])
    await client.latest_bundle()
    assert "authorization" not in httpx_mock.get_requests()[0].headers


# ---------------------------------------------------------------- rate limits


async def test_403_rate_limit_reports_reset_and_is_not_retried(httpx_mock, client):
    reset = int((datetime.now(timezone.utc) + timedelta(minutes=40)).timestamp())
    httpx_mock.add_response(
        url=LIST_URL, status_code=403,
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)},
        json={"message": "API rate limit exceeded"},
    )
    with pytest.raises(RateLimited) as caught:
        await client.latest_bundle()
    assert caught.value.retry_at == datetime.fromtimestamp(reset, tz=timezone.utc)
    assert len(httpx_mock.get_requests()) == 1


async def test_429_uses_retry_after(httpx_mock, client):
    httpx_mock.add_response(url=LIST_URL, status_code=429, headers={"retry-after": "120"})
    before = datetime.now(timezone.utc)
    with pytest.raises(RateLimited) as caught:
        await client.latest_bundle()
    assert timedelta(seconds=119) <= caught.value.retry_at - before <= timedelta(seconds=125)


def test_rate_limit_without_headers_backs_off_an_hour():
    now = datetime(2026, 11, 6, tzinfo=timezone.utc)
    response = httpx.Response(403)
    assert rate_limit_retry_at(response, now) == now + timedelta(hours=1)


async def test_server_errors_are_retried_then_raise(httpx_mock, client):
    httpx_mock.add_response(url=LIST_URL, status_code=502)
    httpx_mock.add_exception(httpx.ConnectError("reset"), url=LIST_URL)
    httpx_mock.add_response(url=LIST_URL, json=[])
    assert await client.latest_bundle() is None
    assert len(httpx_mock.get_requests()) == 3

    httpx_mock.add_response(url=LIST_URL, status_code=500, is_reusable=True)
    with pytest.raises(ReleasesError, match="500"):
        await client.latest_bundle()


async def test_non_json_release_list_is_an_error(httpx_mock, client):
    httpx_mock.add_response(url=LIST_URL, text="<html>")
    with pytest.raises(ReleasesError, match="not JSON"):
        await client.latest_bundle()


# ------------------------------------------------------------------ downloads


async def test_fetch_json_asset_limits_and_errors(httpx_mock, client):
    big = ReleaseAsset(name="manifest.json", size=50, download_url=asset_url("t", "manifest.json"))
    with pytest.raises(ReleasesError, match="limit"):
        await client.fetch_json_asset(big, max_bytes=10)

    lying = ReleaseAsset(name="aliases.json", size=5, download_url=asset_url("t", "aliases.json"))
    httpx_mock.add_response(url=lying.download_url, content=b'{"A": "' + b"B" * 100 + b'"}')
    with pytest.raises(ReleasesError, match="exceeds"):
        await client.fetch_json_asset(lying, max_bytes=50)

    bad = ReleaseAsset(name="x.json", size=3, download_url=asset_url("t", "x.json"))
    httpx_mock.add_response(url=bad.download_url, content=b"nah")
    with pytest.raises(ReleasesError, match="not valid JSON"):
        await client.fetch_json_asset(bad, max_bytes=50)


async def test_fetch_json_asset_retries_transient_failures(httpx_mock, client):
    asset = ReleaseAsset(name="m.json", size=2, download_url=asset_url("t", "m.json"))
    httpx_mock.add_response(url=asset.download_url, status_code=503)
    httpx_mock.add_exception(httpx.ReadError("reset"), url=asset.download_url)
    httpx_mock.add_response(url=asset.download_url, json={"ok": True})
    assert await client.fetch_json_asset(asset, max_bytes=100) == {"ok": True}
    assert len(httpx_mock.get_requests()) == 3


async def test_download_not_found_is_not_retried(httpx_mock, client):
    asset = ReleaseAsset(name="m.json", size=2, download_url=asset_url("t", "m.json"))
    httpx_mock.add_response(url=asset.download_url, status_code=404)
    with pytest.raises(ReleasesError, match="404"):
        await client.fetch_json_asset(asset, max_bytes=100)
    assert len(httpx_mock.get_requests()) == 1
