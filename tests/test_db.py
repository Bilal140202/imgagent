"""Database, models and manifest tests."""

from __future__ import annotations

import pytest

from imgagent.db import Database
from imgagent.errors import NotFoundError
from imgagent.manifest import get_manifest
from imgagent.models import Asset, ImageCandidate, candidate_id_for


def test_candidate_id_stable_and_distinct():
    a1 = candidate_id_for("wikimedia", "https://example/a.jpg")
    a2 = candidate_id_for("wikimedia", "https://example/a.jpg")
    b = candidate_id_for("openverse", "https://example/a.jpg")
    assert a1 == a2 and a1 != b and a1.startswith("cand-")


def test_candidate_roundtrip():
    c = ImageCandidate(provider="wikimedia", image_url="https://x/1.jpg", search_query="q",
                       title="t", width=10, height=20)
    c2 = ImageCandidate.from_dict(c.to_dict())
    assert c2.candidate_id == c.candidate_id and c2.width == 10


def test_job_lifecycle(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.migrate()
    job = db.create_job("j-1", "isdal", {"limit": 5})
    assert job.status == "created"
    db.update_job("j-1", status="ranked", summary={"candidates": 2})
    assert db.get_job("j-1").status == "ranked"
    assert db.latest_job().id == "j-1"
    with pytest.raises(NotFoundError):
        db.resolve_job_ref("missing-job")
    db.close()


def test_candidates_upsert_idempotent(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.migrate()
    db.create_job("j-1", "q", {})
    c = ImageCandidate(provider="wikimedia", image_url="https://x/1.jpg", search_query="q",
                       title="t", relevance_score=50.0)
    assert db.upsert_candidates("j-1", [c]) == 1
    db.upsert_candidates("j-1", [c])  # same id again — no duplicate
    cands, total = db.list_candidates("j-1")
    assert total == 1
    # status preserved across re-search (downloaded is not downgraded)
    db.update_candidate_status(c.candidate_id, "downloaded", "asset-1")
    db.upsert_candidates("j-1", [ImageCandidate(provider="wikimedia", image_url="https://x/1.jpg",
                                                search_query="q", title="t", relevance_score=51.0)])
    got = db.get_candidate(c.candidate_id)
    assert got.download_status == "downloaded"
    db.close()


def test_candidate_filters(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.migrate()
    db.create_job("j-1", "q", {})
    cands = [
        ImageCandidate(provider="wikimedia", image_url="https://x/1.jpg", search_query="q",
                       title="a", width=1000, rights_status="verified_public_domain",
                       relevance_score=10),
        ImageCandidate(provider="openverse", image_url="https://y/2.jpg", search_query="q",
                       title="b", width=50, rights_status="unknown", relevance_score=5),
    ]
    db.upsert_candidates("j-1", cands)
    got, total = db.list_candidates("j-1", provider="wikimedia")
    assert total == 1 and got[0].provider == "wikimedia"
    got, _ = db.list_candidates("j-1", min_width=500)
    assert len(got) == 1
    got, _ = db.list_candidates("j-1", rights_status="verified_public_domain")
    assert len(got) == 1
    db.close()


def test_asset_unique_by_hash_and_manifest(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.migrate()
    db.create_job("j-1", "q", {})
    c = ImageCandidate(provider="wikimedia", image_url="https://x/1.jpg", search_query="q",
                       title="t", rights_status="verified_public_domain")
    db.upsert_candidates("j-1", [c])
    a = Asset(asset_id="asset-1", sha256="f" * 64, path=str(tmp_path / "a.jpg"), byte_size=10,
              downloaded_at="now", candidate_id=c.candidate_id, job_id="j-1",
              source_url="https://x/1.jpg", provider="wikimedia")
    db.record_asset(a)
    db.record_asset(a)  # INSERT OR IGNORE
    assets, total = db.list_assets("j-1")
    assert total == 1
    manifest = get_manifest(db, "j-1")
    assert manifest["schema_version"] == "imgagent.manifest/v1"
    assert manifest["assets"][0]["file"]["sha256"] == "f" * 64
    assert manifest["assets"][0]["provenance"]["source_url"] == "https://x/1.jpg"
    db.close()


def test_source_events_provenance(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.migrate()
    db.create_job("j-1", "q", {})
    db.log_source_event("j-1", "duckduckgo", "source_error", {"error": "vqd missing"})
    events = db.get_source_events("j-1")
    assert events[0]["source"] == "duckduckgo" and events[0]["event"] == "source_error"
    db.close()
