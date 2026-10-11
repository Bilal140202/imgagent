"""Source adapter tests: normalization, license mapping, generic extraction, custom API."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from imgagent import constants as C
from imgagent.config import CustomSourceConfig
from imgagent.errors import ProviderError
from imgagent.http import HttpClient, HttpConfig
from imgagent.security import SecurityOptions
from imgagent.sources.base import SearchOptions
from imgagent.sources.custom import CustomApiSource
from imgagent.sources.generic import GenericSource
from imgagent.sources.openverse import OpenverseSource, map_openverse_license
from imgagent.sources.wikimedia import WikimediaSource, map_license

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def http():
    client = HttpClient(HttpConfig(security=SecurityOptions(allow_private_addr=True),
                                   retries=0, connect_timeout=3, read_timeout=5, cache_ttl=0))
    yield client
    client.close()


# --- wikimedia normalization (offline, fixture-driven) ---------------------------
def test_wikimedia_normalize_page():
    src = WikimediaSource.__new__(WikimediaSource)  # no network
    data = json.loads((FIXTURES / "wikimedia_search.json").read_text())
    pages = data["query"]["pages"]
    jpg = src._normalize(pages[0], "isdal woman")
    assert jpg.provider == "wikimedia"
    assert jpg.image_url.endswith("Isdal_Woman_1970.jpg")
    assert jpg.title == "Isdal Woman 1970"
    assert jpg.width == 2400 and jpg.height == 1800 and jpg.format == "JPEG"
    assert jpg.creator == "Bergen Archive"  # HTML stripped
    assert "Isdal Woman" in jpg.description and "<b>" not in jpg.description
    assert jpg.rights_status == C.RIGHTS_VERIFIED_LICENSE
    assert jpg.attribution and "Wikimedia Commons" in jpg.attribution
    png = src._normalize(pages[1], "isdal woman")
    assert png.rights_status == C.RIGHTS_VERIFIED_PD and png.attribution is None
    assert src._normalize(pages[2], "isdal") is None  # pdf filtered


def test_wikimedia_license_mapping():
    assert map_license("Public domain")[0] == C.RIGHTS_VERIFIED_PD
    assert map_license("CC0")[0] == C.RIGHTS_VERIFIED_PD
    assert map_license("CC BY-SA 4.0")[0] == C.RIGHTS_VERIFIED_LICENSE
    assert map_license("Attribution-ShareAlike 4.0")[0] == C.RIGHTS_VERIFIED_LICENSE
    assert map_license("GFDL")[0] == C.RIGHTS_PERMISSION_REQUIRED
    assert map_license(None)[0] == C.RIGHTS_UNKNOWN


def test_wikimedia_search_live_api_mock(http, mock):
    src = WikimediaSource(http, None)
    src._api_url = lambda: f"{mock.base_url}/notfound"  # HTTP 404 -> graceful ProviderError
    with pytest.raises(ProviderError):
        src.search(SearchOptions(query="isdal", limit=5))


# --- openverse ---------------------------------------------------------------------
def test_openverse_normalization():
    data = json.loads((FIXTURES / "openverse_search.json").read_text())
    src = OpenverseSource.__new__(OpenverseSource)
    out = [src._normalize(item, "bergen") for item in data["results"]]
    first = out[0]
    assert first.image_url.endswith("example1.jpg")
    assert first.rights_status == C.RIGHTS_VERIFIED_LICENSE
    assert first.license_name == "CC BY 2.0"  # hmm: code 'by' -> 'BY 2.0'
    assert first.creator == "Flickr User"
    second = out[1]
    assert second.rights_status == C.RIGHTS_VERIFIED_PD
    assert second.creator is None


def test_openverse_license_mapping():
    assert map_openverse_license("cc0")[0] == C.RIGHTS_VERIFIED_PD
    assert map_openverse_license("pdm")[0] == C.RIGHTS_VERIFIED_PD
    status, restrictions = map_openverse_license("by-nc")
    assert status == C.RIGHTS_VERIFIED_LICENSE and "nc" in restrictions
    assert map_openverse_license(None)[0] == C.RIGHTS_UNKNOWN


# --- generic page extraction ----------------------------------------------------------
def test_generic_og_extraction(http, mock):
    src = GenericSource(http, None)
    result = src.inspect(f"{mock.base_url}/page/og")
    assert result.kind == "page"
    urls = [c.image_url for c in result.page_candidates]
    assert any(u.endswith("/img/jpeg") for u in urls)       # og:image
    assert any(u.endswith("/img/png/2") for u in urls)      # json-ld (relative resolved)
    assert any(u.endswith("/img/png/3") for u in urls)      # img tag
    assert len(urls) == len(set(urls))                       # no duplicates
    assert all(c.rights_status == C.RIGHTS_UNKNOWN for c in result.page_candidates)
    og = next(c for c in result.page_candidates if c.image_url.endswith("/img/jpeg"))
    assert og.title == "Mock Page — Isdal Woman archive"
    assert og.width == 120 and og.height == 90


def test_generic_direct_image_url(http, mock):
    src = GenericSource(http, None)
    result = src.inspect(f"{mock.base_url}/img/jpeg")
    assert result.kind == "image" and result.candidate is not None


def test_generic_fetch_error_reported(http):
    src = GenericSource(http, None)
    result = src.inspect("http://127.0.0.1:1/nope")  # port 1 refuses; private allowed in tests
    assert result.kind == "error" and "fetch failed" in result.notes[0]


# --- custom config-driven API ----------------------------------------------------------
def test_custom_api_adapter(http, mock):
    spec = CustomSourceConfig(
        name="mock",
        endpoint=f"{mock.base_url}/api/search",
        params={"q": "{query}", "limit": "{limit}"},
        results_path="items",
        fields={"image_url": "image", "thumbnail_url": "thumb", "title": "title",
                "source_page_url": "page", "width": "width", "height": "height",
                "creator": "author", "license_name": "license", "format": "fmt"},
        rights_status=C.RIGHTS_PERMISSION_REQUIRED,
    )
    src = CustomApiSource(http, spec)
    cands = src.search(SearchOptions(query="isdal", limit=5))
    assert len(cands) == 5
    first = cands[0]
    assert first.provider == "custom:mock"
    assert first.image_url.startswith(mock.base_url)
    assert first.width == 900 and first.title.startswith("Isdal Woman")
    assert first.rights_status == C.RIGHTS_PERMISSION_REQUIRED
    assert first.format == "PNG"


def test_custom_api_bad_results_path(http, mock):
    spec = CustomSourceConfig(name="bad", endpoint=f"{mock.base_url}/broken-endpoint",
                              results_path="not.there", fields={"image_url": "image"})
    with pytest.raises(ProviderError):
        CustomApiSource(http, spec).search(SearchOptions(query="x", limit=2))
