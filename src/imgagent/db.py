"""Transactional SQLite state store: jobs, candidates, assets, source events.

- WAL journal, single connection guarded by a lock (safe for ThreadPool use).
- schema_version in `meta` with forward-only migrations.
- Candidate records keep their full JSON payload so manifests can be exported
  independent of the database schema.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from imgagent.errors import ConfigError, NotFoundError
from imgagent.models import Asset, ImageCandidate, Job
from imgagent.util import now_utc_iso

DB_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
  id           TEXT PRIMARY KEY,
  query        TEXT NOT NULL,
  params_json  TEXT NOT NULL DEFAULT '{}',
  status       TEXT NOT NULL DEFAULT 'created',
  summary_json TEXT NOT NULL DEFAULT '{}',
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (
  candidate_id    TEXT PRIMARY KEY,
  job_id          TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  provider        TEXT NOT NULL,
  image_url       TEXT NOT NULL,
  title           TEXT,
  relevance_score REAL,
  download_status TEXT NOT NULL DEFAULT 'not_downloaded',
  asset_id        TEXT,
  created_at      TEXT NOT NULL,
  data_json       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_candidates_job      ON candidates(job_id);
CREATE INDEX IF NOT EXISTS idx_candidates_provider ON candidates(provider);
CREATE INDEX IF NOT EXISTS idx_candidates_status   ON candidates(download_status);
CREATE TABLE IF NOT EXISTS assets (
  asset_id      TEXT PRIMARY KEY,
  sha256        TEXT NOT NULL UNIQUE,
  candidate_id  TEXT,
  job_id        TEXT,
  path          TEXT NOT NULL,
  byte_size     INTEGER NOT NULL,
  width         INTEGER,
  height        INTEGER,
  format        TEXT,
  project       TEXT,
  scene         TEXT,
  downloaded_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assets_job ON assets(job_id);
CREATE TABLE IF NOT EXISTS source_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id      TEXT,
  source      TEXT NOT NULL,
  event       TEXT NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}',
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_source_events_job ON source_events(job_id);
"""


class Database:
    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    # -- connection -----------------------------------------------------------
    def _get(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(str(self._path), check_same_thread=False)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA synchronous=NORMAL")
                self._conn = conn
            return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._get()
        with self._lock:
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    # -- migrations -------------------------------------------------------------
    def migrate(self) -> int:
        with self._tx() as conn:
            conn.executescript(_SCHEMA)
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            current = int(row["value"]) if row else 0
            if current > DB_SCHEMA_VERSION:
                raise ConfigError(
                    f"database {self._path} was written by a newer imgagent "
                    f"(schema {current} > {DB_SCHEMA_VERSION}); upgrade the package."
                )
            # Future migrations: if current < N: apply migration N
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(DB_SCHEMA_VERSION),),
            )
            return DB_SCHEMA_VERSION

    # -- jobs -------------------------------------------------------------------
    def create_job(self, job_id: str, query: str, params: dict) -> Job:
        ts = now_utc_iso()
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO jobs(id, query, params_json, status, created_at, updated_at) "
                "VALUES(?, ?, ?, 'created', ?, ?)",
                (job_id, query, json.dumps(params, ensure_ascii=False), ts, ts),
            )
        return Job(id=job_id, query=query, status="created", params=params, created_at=ts, updated_at=ts)

    def update_job(self, job_id: str, *, status: str | None = None, summary: dict | None = None) -> None:
        sets, args = ["updated_at=?"], [now_utc_iso()]
        if status is not None:
            sets.append("status=?")
            args.append(status)
        if summary is not None:
            sets.append("summary_json=?")
            args.append(json.dumps(summary, ensure_ascii=False))
        args.append(job_id)
        with self._tx() as conn:
            cur = conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", args)
            if cur.rowcount == 0:
                raise NotFoundError(f"job {job_id!r} not found")

    def get_job(self, job_id: str) -> Job:
        row = self._get().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None  # type: ignore[return-value]
        return Job(
            id=row["id"],
            query=row["query"],
            status=row["status"],
            params=json.loads(row["params_json"]),
            summary=json.loads(row["summary_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def latest_job(self) -> Job | None:
        row = self._get().execute("SELECT id FROM jobs ORDER BY created_at DESC, id DESC LIMIT 1").fetchone()
        return self.get_job(row["id"]) if row else None

    def resolve_job_ref(self, ref: str) -> Job:
        """'latest' | explicit job id."""
        if ref == "latest":
            job = self.latest_job()
            if job is None:
                raise NotFoundError("no jobs yet in this workspace — run a search first")
            return job
        job = self.get_job(ref)
        if job is None:
            raise NotFoundError(f"job {ref!r} not found")
        return job

    def list_jobs(self, limit: int = 20) -> list[Job]:
        rows = self._get().execute(
            "SELECT id FROM jobs ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self.get_job(r["id"]) for r in rows]

    # -- candidates ---------------------------------------------------------------
    def upsert_candidates(self, job_id: str, candidates: list[ImageCandidate]) -> int:
        ts = now_utc_iso()
        with self._tx() as conn:
            for c in candidates:
                conn.execute(
                    "INSERT INTO candidates(candidate_id, job_id, provider, image_url, title, "
                    "relevance_score, download_status, created_at, data_json) "
                    "VALUES(?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(candidate_id) DO UPDATE SET "
                    "  job_id=excluded.job_id, "
                    "  title=excluded.title, relevance_score=excluded.relevance_score, "
                    "  download_status=MIN(candidates.download_status, excluded.download_status), "
                    "  asset_id=COALESCE(candidates.asset_id, excluded.asset_id), "
                    "  data_json=json_set(excluded.data_json, "
                    "    '$.download_status', MIN(candidates.download_status, excluded.download_status), "
                    "    '$.asset_id', COALESCE(candidates.asset_id, json_extract(excluded.data_json, '$.asset_id')))",
                    (
                        c.candidate_id, job_id, c.provider, c.image_url, c.title,
                        c.relevance_score, c.download_status, ts, json.dumps(c.to_dict(), ensure_ascii=False),
                    ),
                )
            return len(candidates)

    def update_candidate_status(self, candidate_id: str, status: str, asset_id: str | None = None) -> None:
        """Update status columns AND the JSON payload (single source of truth for readers)."""
        with self._tx() as conn:
            if asset_id:
                conn.execute(
                    "UPDATE candidates SET download_status=?, asset_id=?, "
                    "data_json=json_set(data_json, '$.download_status', ?, '$.extra.asset_id', ?) "
                    "WHERE candidate_id=?",
                    (status, asset_id, status, asset_id, candidate_id),
                )
            else:
                conn.execute(
                    "UPDATE candidates SET download_status=?, "
                    "data_json=json_set(data_json, '$.download_status', ?) WHERE candidate_id=?",
                    (status, status, candidate_id),
                )

    def get_candidate(self, candidate_id: str) -> ImageCandidate:
        row = self._get().execute(
            "SELECT data_json FROM candidates WHERE candidate_id=?", (candidate_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"candidate {candidate_id!r} not found")
        return ImageCandidate.from_dict(json.loads(row["data_json"]))

    def get_candidates_by_ids(self, ids: list[str]) -> list[ImageCandidate]:
        out = []
        for cid in ids:
            row = self._get().execute(
                "SELECT data_json FROM candidates WHERE candidate_id=?", (cid,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"candidate {cid!r} not found")
            out.append(ImageCandidate.from_dict(json.loads(row["data_json"])))
        return out

    def list_candidates(
        self,
        job_id: str | None = None,
        *,
        provider: str | None = None,
        downloaded: bool | None = None,
        rights_status: str | None = None,
        min_width: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ImageCandidate], int]:
        where, args = [], []
        if job_id:
            where.append("job_id=?")
            args.append(job_id)
        if provider:
            where.append("provider=?")
            args.append(provider)
        if downloaded is True:
            where.append("download_status IN ('downloaded','duplicate')")
        elif downloaded is False:
            where.append("download_status='not_downloaded'")
        if rights_status:
            where.append("json_extract(data_json, '$.rights_status')=?")
            args.append(rights_status)
        if min_width:
            where.append("json_extract(data_json, '$.width') >= ?")
            args.append(min_width)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        conn = self._get()
        total = conn.execute(f"SELECT COUNT(*) AS n FROM candidates {clause}", args).fetchone()["n"]
        rows = conn.execute(
            f"SELECT data_json FROM candidates {clause} "
            "ORDER BY relevance_score DESC, candidate_id ASC LIMIT ? OFFSET ?",
            [*args, limit, offset],
        ).fetchall()
        return [ImageCandidate.from_dict(json.loads(r["data_json"])) for r in rows], total

    # -- assets ---------------------------------------------------------------------
    def record_asset(self, asset: Asset) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO assets(asset_id, sha256, candidate_id, job_id, path, byte_size, "
                "width, height, format, project, scene, downloaded_at, metadata_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    asset.asset_id, asset.sha256, asset.candidate_id, asset.job_id, asset.path,
                    asset.byte_size, asset.width, asset.height, asset.format, asset.project,
                    asset.scene, asset.downloaded_at, json.dumps(asset.to_dict(), ensure_ascii=False),
                ),
            )

    def get_asset(self, asset_id: str) -> Asset | None:
        row = self._get().execute(
            "SELECT metadata_json FROM assets WHERE asset_id=?", (asset_id,)
        ).fetchone()
        return Asset(**json.loads(row["metadata_json"])) if row else None

    def get_asset_by_hash(self, sha256_hex: str) -> Asset | None:
        row = self._get().execute(
            "SELECT metadata_json FROM assets WHERE sha256=?", (sha256_hex,)
        ).fetchone()
        return Asset(**json.loads(row["metadata_json"])) if row else None

    def list_assets(self, job_id: str | None = None, limit: int = 200, offset: int = 0) -> tuple[list[Asset], int]:
        where, args = ([], [])
        if job_id:
            where.append("job_id=?")
            args.append(job_id)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        conn = self._get()
        total = conn.execute(f"SELECT COUNT(*) AS n FROM assets {clause}", args).fetchone()["n"]
        rows = conn.execute(
            f"SELECT metadata_json FROM assets {clause} ORDER BY downloaded_at DESC LIMIT ? OFFSET ?",
            [*args, limit, offset],
        ).fetchall()
        return [Asset(**json.loads(r["metadata_json"])) for r in rows], total

    def all_asset_paths(self) -> set[str]:
        rows = self._get().execute("SELECT path FROM assets").fetchall()
        return {r["path"] for r in rows}

    # -- source events (provenance trace per job) -------------------------------------
    def log_source_event(self, job_id: str | None, source: str, event: str, detail: dict | None = None) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO source_events(job_id, source, event, detail_json, created_at) VALUES(?,?,?,?,?)",
                (job_id, source, event, json.dumps(detail or {}, ensure_ascii=False), now_utc_iso()),
            )

    def get_source_events(self, job_id: str) -> list[dict]:
        rows = self._get().execute(
            "SELECT source, event, detail_json, created_at FROM source_events WHERE job_id=? ORDER BY id",
            (job_id,),
        ).fetchall()
        return [
            {"source": r["source"], "event": r["event"], "detail": json.loads(r["detail_json"]), "at": r["created_at"]}
            for r in rows
        ]

    # -- maintenance -------------------------------------------------------------------
    def counts(self) -> dict[str, int]:
        conn = self._get()
        return {
            "jobs": conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"],
            "candidates": conn.execute("SELECT COUNT(*) n FROM candidates").fetchone()["n"],
            "assets": conn.execute("SELECT COUNT(*) n FROM assets").fetchone()["n"],
        }

    def storage_usage(self) -> dict[str, int]:
        conn = self._get()
        row = conn.execute("SELECT COALESCE(SUM(byte_size),0) n FROM assets").fetchone()
        db_size = self._path.stat().st_size if self._path.exists() else 0
        return {"assets_bytes": int(row["n"]), "database_bytes": db_size}

    def clear_cache_table(self) -> None:  # pragma: no cover - placeholder for future tables
        pass


def db_supports_json1() -> bool:
    try:
        conn = sqlite3.connect(":memory:")
        conn.execute("SELECT json_extract('{\"a\":1}', '$.a')").fetchone()
        conn.close()
        return True
    except sqlite3.Error:  # pragma: no cover
        return False
