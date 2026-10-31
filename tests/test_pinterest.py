"""Pinterest source tests: normalization, SSR fallback, graceful failure, registration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from imgagent import constants as C
from imgagent.config import DEFAULT_SOURCES, SourceConfig
from imgagent.errors import ProviderError
from imgagent.http import HttpClient, HttpConfig
from imgagent.security import SecurityOptions
from imgagent.sources import available_sources, build_source
from imgagent.sources.base import SearchOptions
from imgagent.sources.pinterest import PinterestSource

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def http():
    client = HttpClient(HttpConfig(security=SecurityOptions(allow_private_addr=True),
                                   retries=0, connect_timeout=3, read_timeout=5, cache_ttl=0))
    yield client
    client.close()


# --- normalization (offline, fixture-driven) -------------------------------------
def test_pinterest_normalize():
    src = PinterestSource.__new__(PinterestSource)  # no network
    data = json.loads((FIXTURES / "pinterest_search.json").read_text())
    items = data["resource_response"]["data"]
    pins = [c for c in (src._normalize(item, "norway fjord", "resource_endpoint")
                        for item in items) if c]
    assert len(pins) == 2  # board module, ad module and external-host pin filtered

    first = pins[0]
    assert first.provider == "pinterest"
    assert first.image_url == ("https://i.pinimg.com/originals/cd/e8/17/"
                               "cde81773edc15fa114c9e38a7c66cc5f.png")
    assert first.format == "PNG"
    assert first.width == 1000 and first.height == 1500
    assert first.title == "Norway Fjord Village"
    assert first.description == "A village at the end of a fjord"
    assert first.source_page_url == "https://www.pinterest.com/pin/1477812317536479/"
    assert first.thumbnail_url == ("https://i.pinimg.com/236x/cd/e8/17/"
                                   "cde81773edc15fa114c9e38a7c66cc5f.jpg")
    assert first.rights_status == C.RIGHTS_UNKNOWN  # never inferred
    assert first.extra["via"] == "resource_endpoint"
    assert first.extra["repin_count"] == 42
    assert first.extra["outer_link"] == "https://example.org/travel/norway"
    assert first.extra["board"] == "Norway Travel"
    assert first.extra["pinner"] == "fjordfan"

    second = pins[1]
    assert second.image_url == ("https://i.pinimg.com/736x/ab/cd/ef/"
                                "abcdef1234567890abcdef1234567890.jpg")  # orig missing -> 736x
    assert second.title == "Geirangerfjord sunset"  # grid_title fallback
    assert second.description == "sunset over the fjord"  # auto_alt_text fallback
    assert second.width == 736 and second.height == 1104
    assert second.extra == {"via": "resource_endpoint"}  # nothing invented


def test_pinterest_normalize_rejects_junk():
    src = PinterestSource.__new__(PinterestSource)
    assert src._normalize({}, "q", "resource_endpoint") is None
    assert src._normalize({"images": {}}, "q", "resource_endpoint") is None
    bad_host = {"id": "1", "images": {"orig": {"url": "https://evil.example/a.jpg"}}}
    assert src._normalize(bad_host, "q", "resource_endpoint") is None
    # unknown extension -> format None (downloader sniffs), candidate kept
    odd = {"id": "2", "images": {"orig": {"url": "https://i.pinimg.com/originals/x/y/z/xyz.heic"}}}
    cand = src._normalize(odd, "q", "resource_endpoint")
    assert cand is not None and cand.format is None


# --- strategy A: resource endpoint via mock ----------------------------------------
def test_pinterest_resource_endpoint_mock(http, mock):
    src = PinterestSource(http, SourceConfig(name="pinterest", api_url=mock.base_url))
    out = src.search(SearchOptions(query="norway fjord", limit=5))
    assert len(out) == 2
    assert all(c.extra["via"] == "resource_endpoint" for c in out)
    assert all(c.rights_status == C.RIGHTS_UNKNOWN for c in out)
    assert src.health_check() is True


def test_pinterest_health_check_failure_path(http):
    src = PinterestSource(http, SourceConfig(name="pinterest", api_url="http://127.0.0.1:9"))
    assert src.health_check() is False


# --- strategy B: SSR fallback after strategy A is blocked ---------------------------
def test_pinterest_ssr_fallback(http, mock, monkeypatch):
    src = PinterestSource(http, SourceConfig(name="pinterest", api_url=mock.base_url))

    def blocked(*args, **kwargs):
        raise ProviderError("resource endpoint: HTTP 403")

    monkeypatch.setattr(src.http, "get_json", blocked)
    out = src.search(SearchOptions(query="norway fjord", limit=5))
    assert len(out) == 2
    assert all(c.extra["via"] == "ssr_html" for c in out)
    assert out[0].source_page_url == "https://www.pinterest.com/pin/111222333444555/"


def test_pinterest_softgate_envelope_falls_back(http, mock):
    """Live-observed variant: HTTP 200 + empty results envelope on strategy A
    (a /pinterest-prefixed base_url) must fall through to strategy B."""
    src = PinterestSource(http, SourceConfig(name="pinterest", api_url=f"{mock.base_url}/pinterest"))
    out = src.search(SearchOptions(query="norway fjord", limit=5))
    assert len(out) == 2
    assert all(c.extra["via"] == "ssr_html" for c in out)


def test_pinterest_softgate_with_shell_raises(http, mock):
    src = PinterestSource(http, SourceConfig(name="pinterest", api_url=f"{mock.base_url}/pinterest"))
    with pytest.raises(ProviderError) as excinfo:
        src.search(SearchOptions(query="shelltest", limit=3))
    msg = str(excinfo.value)
    assert "soft gate" in msg and "app shell" in msg


def test_pinterest_app_shell_is_provider_error(http, mock, monkeypatch):
    src = PinterestSource(http, SourceConfig(name="pinterest", api_url=mock.base_url))

    def blocked(*args, **kwargs):
        raise ProviderError("resource endpoint: HTTP 403")

    monkeypatch.setattr(src.http, "get_json", blocked)
    with pytest.raises(ProviderError) as excinfo:
        src.search(SearchOptions(query="shelltest", limit=3))
    msg = str(excinfo.value)
    assert "app shell" in msg and "resource endpoint failed" in msg


def test_pinterest_both_strategies_fail(http):
    src = PinterestSource(http, SourceConfig(name="pinterest", api_url="http://127.0.0.1:9"))
    with pytest.raises(ProviderError) as excinfo:
        src.search(SearchOptions(query="x", limit=3))
    msg = str(excinfo.value)
    assert "resource endpoint failed" in msg and "SSR fallback failed" in msg


# --- registration & policy ----------------------------------------------------------
def test_pinterest_registered():
    from imgagent.http import HttpClient as _H  # local import keeps fixture optional
    client = _H(HttpConfig(security=SecurityOptions(allow_private_addr=True), cache_ttl=0))
    try:
        src = build_source("pinterest", client, None)
        assert isinstance(src, PinterestSource)
        caps = src.capabilities()
        assert caps.best_effort is True
        assert caps.requires_api_key is False
        assert caps.supports_search is True
        assert "pinterest" in available_sources()
    finally:
        client.close()


def test_pinterest_not_in_default_sources():
    assert "pinterest" not in DEFAULT_SOURCES


def test_pinterest_base_url_from_config(http):
    src = PinterestSource(http, SourceConfig(
        name="pinterest", api_url="https://www.pinterest.de", options={"page_size": 10}))
    assert src._base_url() == "https://www.pinterest.de"
