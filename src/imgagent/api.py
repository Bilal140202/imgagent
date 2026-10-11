"""Programmatic API — stable function contracts (spec section 12).

All functions are synchronous, return plain dicts (JSON-safe), raise the
imgagent error taxonomy, and share the exact business logic used by the CLI —
no separate implementation. Progress callbacks receive structured events and
must never write to stdout (JSON purity is owned by the caller).

Contracts:
- search_images(query, options) -> {"job_id", "candidates", "errors"}
- list_candidates(job_id, filters, pagination) -> {"job", "total", "candidates"}
- inspect_image_url(url, options) -> InspectResult.to_dict()
- download_image(candidate_id, destination, options) -> DownloadResult dict
- download_selected(candidate_ids, destination, options) -> {"job_id"?, results, summary}
- run_image_job(query, options) -> full run payload (see README)
- get_job(job_id) -> job dict
- get_manifest(job_id) -> manifest dict
- get_asset_metadata(asset_id) -> asset dict
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from imgagent.config import Config, load_config
from imgagent.db import Database
from imgagent.downloader import Downloader, DownloadOptions, summarize_downloads
from imgagent.errors import NotFoundError, UsageError
from imgagent.jobs import Orchestrator
from imgagent.manifest import get_manifest as _get_manifest
from imgagent.workspace import Workspace

ProgressFn = Callable[[dict], None]


def _resolve(workspace: Path | str | None) -> tuple[Config, Workspace, Database]:
    cfg = load_config(Path(workspace).expanduser() if workspace else None)
    ws = Workspace.from_config(cfg).ensure()
    db = Database(ws.db_path)
    db.migrate()
    return cfg, ws, db


def search_images(query: str, *, limit: int = 20, sources: list[str] | None = None,
                  min_width: int | None = None, min_height: int | None = None,
                  max_bytes: int | None = None, require_reuse_rights: bool | None = None,
                  prefer_authentic: bool | None = None,
                  workspace: Path | str | None = None) -> dict[str, Any]:
    cfg, _ws, _db = _resolve(workspace)
    orch = Orchestrator(cfg)
    try:
        job_id, ranked, errors = orch.search_only(
            query, limit=limit, sources=sources, min_width=min_width, min_height=min_height,
            max_bytes=max_bytes, require_reuse_rights=require_reuse_rights,
            prefer_authentic=prefer_authentic)
        return {"job_id": job_id, "candidates": [c.to_dict() for c in ranked], "errors": errors}
    finally:
        orch.close()


def list_candidates(job_id: str = "latest", *, provider: str | None = None,
                    downloaded: bool | None = None, rights_status: str | None = None,
                    min_width: int | None = None, limit: int = 50, offset: int = 0,
                    workspace: Path | str | None = None) -> dict[str, Any]:
    _cfg, _ws, db = _resolve(workspace)
    try:
        job = db.resolve_job_ref(job_id)
        cands, total = db.list_candidates(
            job.id, provider=provider, downloaded=downloaded, rights_status=rights_status,
            min_width=min_width, limit=limit, offset=offset)
        return {"job": job.to_dict(), "total": total, "offset": offset, "limit": limit,
                "candidates": [c.to_dict() for c in cands]}
    finally:
        db.close()


def inspect_image_url(url: str, *, workspace: Path | str | None = None) -> dict[str, Any]:
    from imgagent.sources import build_source
    from imgagent.sources.generic import GenericSource
    cfg, _ws, _db = _resolve(workspace)
    orch = Orchestrator(cfg)
    try:
        if url.startswith(("http://", "https://")) and "commons.wikimedia.org/wiki/File:" in url:
            result = build_source("wikimedia", orch.http, cfg).inspect(url)
            if result.candidate is not None:
                return result.to_dict()
        return GenericSource(orch.http, cfg).inspect(url).to_dict()
    finally:
        orch.close()


def download_image(candidate_id: str, destination: str | Path | None = None, *,
                   project: str | None = None, scene: str | None = None,
                   workspace: Path | str | None = None,
                   progress: ProgressFn | None = None) -> dict[str, Any]:
    return download_selected([candidate_id], destination, project=project, scene=scene,
                             workspace=workspace, progress=progress)


def download_selected(candidate_ids: list[str], destination: str | Path | None = None, *,
                      project: str | None = None, scene: str | None = None,
                      workspace: Path | str | None = None,
                      progress: ProgressFn | None = None) -> dict[str, Any]:
    cfg, ws, db = _resolve(workspace)
    orch = Orchestrator(cfg, db=db, ws=ws)
    try:
        candidates = db.get_candidates_by_ids(list(candidate_ids))
        if not candidates:
            raise UsageError("no candidate ids given")
        if destination:
            cfg.assets_root = Path(destination).expanduser()
            ws.assets_root = cfg.assets_root
        opts = DownloadOptions(
            project=project or cfg.default_project, scene=scene, concurrency=cfg.concurrency,
            max_bytes=cfg.max_bytes, max_pixels=cfg.max_pixels, thumbnails=cfg.thumbnails,
            thumb_size=cfg.thumb_size,
        )
        dl = Downloader(orch.http, ws, db)
        results = dl.download_many(candidates, opts, on_result=(lambda r: progress(r.to_dict()) if progress else None))
        summary = summarize_downloads(results)
        return {"results": [r.to_dict() for r in results], "summary": summary}
    finally:
        orch.close()


def run_image_job(query: str, *, limit: int = 20, top: int = 5, sources: list[str] | None = None,
                  min_width: int | None = None, min_height: int | None = None,
                  max_bytes: int | None = None, require_reuse_rights: bool | None = None,
                  prefer_authentic: bool | None = None, project: str | None = None,
                  scene: str | None = None, destination: str | Path | None = None,
                  workspace: Path | str | None = None,
                  progress: ProgressFn | None = None) -> dict[str, Any]:
    cfg, _ws, _db = _resolve(workspace)
    orch = Orchestrator(cfg)
    try:
        payload = orch.run(query, limit=limit, top=top, sources=sources, min_width=min_width,
                           min_height=min_height, max_bytes=max_bytes,
                           require_reuse_rights=require_reuse_rights,
                           prefer_authentic=prefer_authentic, project=project, scene=scene,
                           out_root=str(destination) if destination else None)
        if progress and payload.get("download_results"):
            for r in payload["download_results"]:
                progress(r)
        return payload
    finally:
        orch.close()


def get_job(job_id: str = "latest", *, workspace: Path | str | None = None) -> dict[str, Any]:
    _cfg, _ws, db = _resolve(workspace)
    try:
        job = db.resolve_job_ref(job_id)
        job_dict = job.to_dict()
        job_dict["source_events"] = db.get_source_events(job.id)
        return job_dict
    finally:
        db.close()


def get_manifest(job_id: str = "latest", *, workspace: Path | str | None = None) -> dict[str, Any]:
    _cfg, ws, db = _resolve(workspace)
    try:
        job = db.resolve_job_ref(job_id)
        manifest = _get_manifest(db, job.id)
        if manifest is None:
            raise NotFoundError(f"no manifest for job {job.id}")
        return manifest
    finally:
        db.close()


def get_asset_metadata(asset_id: str, *, workspace: Path | str | None = None) -> dict[str, Any]:
    _cfg, _ws, db = _resolve(workspace)
    try:
        asset = db.get_asset(asset_id)
        if asset is None:
            raise NotFoundError(f"asset {asset_id!r} not found")
        return asset.to_dict()
    finally:
        db.close()
