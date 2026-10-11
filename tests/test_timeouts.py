"""Timeout behaviour: slow endpoints must fail with bounded latency after retries."""

from __future__ import annotations

import time

from imgagent.config import Config
from imgagent.db import Database
from imgagent.downloader import Downloader, DownloadOptions
from imgagent.http import HttpClient, HttpConfig
from imgagent.models import ImageCandidate
from imgagent.security import SecurityOptions
from imgagent.workspace import Workspace


def test_slow_endpoint_times_out_bounded(mock, tmp_path):
    cfg = Config(workspace=tmp_path / ".imgagent", assets_root=tmp_path / "assets")
    ws = Workspace.from_config(cfg).ensure()
    db = Database(ws.db_path)
    db.migrate()
    http = HttpClient(HttpConfig(security=SecurityOptions(allow_private_addr=True),
                                 retries=1, connect_timeout=2, read_timeout=0.5, cache_ttl=0))
    dl = Downloader(http, ws, db)
    cand = ImageCandidate(provider="mock", image_url=f"{mock.base_url}/img/slow",
                          search_query="t", title="slow")
    start = time.time()
    res = dl.download_one(cand, DownloadOptions(max_bytes=1_000_000))
    elapsed = time.time() - start
    assert res.status == "failed"
    assert res.error_kind == "download_error"
    # 2 attempts x 0.5s read timeout + backoff — far below the 2s server sleep total
    assert elapsed < 8
    http.close()
    db.close()
