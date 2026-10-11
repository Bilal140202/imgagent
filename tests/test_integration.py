"""Opt-in live-source integration tests (skipped by default).

Run with:  pytest -m integration
These hit real public APIs (Wikimedia Commons / Openverse). They assert only
contract stability, never volumes: provider contents change over time.
"""

from __future__ import annotations

import pytest

from imgagent.config import load_config
from imgagent.jobs import Orchestrator
from imgagent.sources.base import SearchOptions
from imgagent.sources.openverse import OpenverseSource
from imgagent.sources.wikimedia import WikimediaSource


@pytest.mark.integration
def test_wikimedia_live_search():
    cfg = load_config()
    orch = Orchestrator(cfg)
    try:
        src = WikimediaSource(orch.http, cfg.source_configs.get("wikimedia"))
        cands = src.search(SearchOptions(query="Isdal Woman", limit=5))
        assert cands, "wikimedia returned zero results for a well-known query"
        first = cands[0]
        assert first.image_url.startswith("https://upload.wikimedia.org/")
        assert first.source_page_url and first.width and first.height
        assert first.provider == "wikimedia"
        # download the smallest original fully and verify bytes
        from imgagent.downloader import Downloader, DownloadOptions
        opts = DownloadOptions(max_bytes=30_000_000)
        dl = Downloader(orch.http, orch.ws, orch.db)
        res = dl.download_one(min(cands, key=lambda c: c.byte_size or 1 << 40), opts)
        assert res.status in ("downloaded", "duplicate"), res.error  # duplicate = valid hash reuse
        assert res.sha256 and res.width
    finally:
        orch.close()


@pytest.mark.integration
def test_openverse_live_search():
    cfg = load_config()
    orch = Orchestrator(cfg)
    try:
        src = OpenverseSource(orch.http, cfg.source_configs.get("openverse"))
        cands = src.search(SearchOptions(query="bergen norway", limit=5))
        assert cands, "openverse returned zero results for a well-known query"
        first = cands[0]
        assert first.license_name, "openverse results must carry license metadata"
        assert first.rights_status in ("verified_public_domain", "verified_license")
    finally:
        orch.close()


@pytest.mark.integration
def test_full_live_run_requires_reuse_rights():
    cfg = load_config(cli_overrides={"require_reuse_rights": True})
    orch = Orchestrator(cfg)
    try:
        job_id, ranked, errors = orch.search_only("Bergen Norway 1970 harbor", limit=10,
                                                  sources=["wikimedia", "openverse"])
        assert ranked, "no rights-cleared candidates for a generic historical query"
        assert all(c.rights_status in ("verified_public_domain", "verified_license")
                   for c in ranked)
    finally:
        orch.close()
