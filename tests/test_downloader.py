"""Download engine tests: validation, retries, atomicity, dedup, resume."""

from __future__ import annotations

import os

import pytest

from imgagent.config import Config
from imgagent.db import Database
from imgagent.downloader import Downloader, DownloadOptions
from imgagent.http import HttpClient, HttpConfig
from imgagent.images import average_hash, hamming_hex
from imgagent.models import ImageCandidate
from imgagent.security import SecurityOptions
from imgagent.workspace import Workspace
from mock_server import make_png


def _candidate(mock, path: str, title="test image") -> ImageCandidate:
    return ImageCandidate(provider="mock", image_url=f"{mock.base_url}{path}",
                          search_query="test", title=title)


def _registered(db, mock, path: str, title="test image") -> ImageCandidate:
    """Candidate registered in the DB, mirroring the real search->download flow."""
    c = _candidate(mock, path, title)
    db.create_job("j-test", "test", {})
    db.upsert_candidates("j-test", [c])
    return c


@pytest.fixture()
def env(tmp_path):
    cfg = Config(workspace=tmp_path / ".imgagent", assets_root=tmp_path / "assets")
    ws = Workspace.from_config(cfg).ensure()
    db = Database(ws.db_path)
    db.migrate()
    http = HttpClient(HttpConfig(
        security=SecurityOptions(allow_private_addr=True), retries=3,
        connect_timeout=3, read_timeout=5, cache_ttl=0))
    opts = DownloadOptions(max_bytes=1024 * 1024, thumbnails=False)
    yield cfg, ws, db, http, opts
    http.close()
    db.close()


def test_valid_png_download(env, mock):
    cfg, ws, db, http, opts = env
    dl = Downloader(http, ws, db)
    cand = _registered(db, mock, "/img/png/1")
    res = dl.download_one(cand, opts)
    assert res.status == "downloaded"
    assert res.format == "PNG" and res.width == 90 and res.height == 65
    assert os.path.exists(res.path)
    assert len(res.sha256) == 64
    # candidate marked downloaded only after validation
    c = db.get_candidate(res.candidate_id)
    assert c.download_status == "downloaded" and c.extra.get("asset_id")


def test_jpeg_webp_gif_downloads(env, mock):
    cfg, ws, db, http, opts = env
    dl = Downloader(http, ws, db)
    for path, fmt in (("/img/jpeg", "JPEG"), ("/img/webp", "WEBP"), ("/img/gif", "GIF")):
        res = dl.download_one(_candidate(mock, path), opts)
        assert res.status == "downloaded", path
        assert res.format == fmt, path


def test_html_masquerading_as_image_rejected(env, mock):
    cfg, ws, db, http, opts = env
    dl = Downloader(http, ws, db)
    res = dl.download_one(_candidate(mock, "/img/html-as-image"), opts)
    assert res.status == "failed"
    assert res.error_kind == "validation_failed"
    leftovers = list((ws.assets_root / "default" / "originals").glob("*"))
    assert not leftovers  # .part cleaned up


def test_corrupt_image_rejected(env, mock):
    cfg, ws, db, http, opts = env
    dl = Downloader(http, ws, db)
    res = dl.download_one(_candidate(mock, "/img/corrupt"), opts)
    assert res.status == "failed"
    assert res.error_kind == "validation_failed"


def test_empty_file_rejected(env, mock):
    cfg, ws, db, http, opts = env
    dl = Downloader(http, ws, db)
    res = dl.download_one(_candidate(mock, "/img/empty"), opts)
    assert res.status == "failed" and res.error_kind == "validation_failed"


def test_oversized_download_rejected(env, mock):
    cfg, ws, db, http, opts = env
    dl = Downloader(http, ws, db)
    res = dl.download_one(_candidate(mock, "/img/big"), opts)
    assert res.status == "failed"
    assert "max_bytes" in res.error


def test_404_fails_without_retry_loop(env, mock):
    cfg, ws, db, http, opts = env
    dl = Downloader(http, ws, db)
    res = dl.download_one(_candidate(mock, "/always404"), opts)
    assert res.status == "failed"
    assert mock.counts["/always404"] == 1  # 404 is not retryable


def test_retry_then_success(env, mock):
    cfg, ws, db, http, opts = env
    dl = Downloader(http, ws, db)
    res = dl.download_one(_candidate(mock, "/flaky/2"), opts)
    assert res.status == "downloaded"
    assert mock.counts["/flaky/2"] == 3  # two 500s + success


def test_retries_exhausted_fails(env, mock):
    cfg, ws, db, http, opts = env
    dl = Downloader(http, ws, db)
    res = dl.download_one(_candidate(mock, "/always500"), opts)
    assert res.status == "failed"
    assert mock.counts["/always500"] == 4  # retries=3 + initial


def test_exact_dedup_by_hash(env, mock):
    cfg, ws, db, http, opts = env
    dl = Downloader(http, ws, db)
    r1 = dl.download_one(_candidate(mock, "/img/png/4", "first title"), opts)
    r2 = dl.download_one(_candidate(mock, "/img/png/4", "second title"), opts)
    assert r1.status == "downloaded" and r2.status == "duplicate"
    assert r1.asset_id == r2.asset_id
    files = list((ws.assets_root / "default" / "originals").glob("*.png"))
    assert len(files) == 1


def test_repeated_download_skips_existing(env, mock):
    cfg, ws, db, http, opts = env
    dl = Downloader(http, ws, db)
    cand = _registered(db, mock, "/img/png/5")
    r1 = dl.download_one(cand, opts)
    cand2 = db.get_candidate(cand.candidate_id)
    r2 = dl.download_one(cand2, opts)
    assert r2.status == "skipped_exists"
    assert r2.asset_id == r1.asset_id


def test_metadata_jsonl_written(env, mock):
    cfg, ws, db, http, opts = env
    dl = Downloader(http, ws, db)
    dl.download_one(_candidate(mock, "/img/png/6"), opts)
    from imgagent.workspace import MetadataLog
    records = MetadataLog(ws.metadata_jsonl("default")).read_all()
    assert len(records) == 1
    assert records[0]["sha256"] and records[0]["rights_status"] == "unknown"


def test_thumbnail_generated(env, mock):
    cfg, ws, db, http, opts = env
    opts.thumbnails = True
    dl = Downloader(http, ws, db)
    res = dl.download_one(_candidate(mock, "/img/png/7"), opts)
    thumbs = list((ws.assets_root / "default" / "thumbnails").glob("*.jpg"))
    assert res.status == "downloaded" and len(thumbs) == 1


def test_perceptual_hash_distance():
    import io

    from PIL import Image

    a = Image.open(io.BytesIO(make_png(100, 100)))
    b = a.copy().rotate(2, fillcolor=(0, 0, 0))  # slight variation
    h1, h2 = average_hash(a), average_hash(b)
    assert hamming_hex(h1, h2) <= 8  # near-duplicate territory
    c = Image.new("RGB", (100, 100), (250, 250, 250))
    assert hamming_hex(h1, average_hash(c)) > 8  # distinct image


def test_concurrent_download_many(env, mock):
    cfg, ws, db, http, opts = env
    opts.concurrency = 4
    dl = Downloader(http, ws, db)
    cands = [_candidate(mock, f"/img/png/{i}") for i in range(1, 6)]
    results = dl.download_many(cands, opts)
    assert len(results) == 5
    assert all(r.status == "downloaded" for r in results)
