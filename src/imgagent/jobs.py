"""Job orchestrator: search -> normalize -> rank -> dedup -> (download) -> manifest.

One command supports the whole workflow; every stage is also independently
callable (search_only / download_from_job / get_manifest...).

Per-source isolation: each source runs inside try/except and its failure is
recorded as a structured error — other sources are unaffected.
"""

from __future__ import annotations

from typing import Any

from imgagent import constants as C
from imgagent.config import Config
from imgagent.db import Database
from imgagent.downloader import Downloader, DownloadOptions, summarize_downloads
from imgagent.errors import NotFoundError, ProviderError, UsageError
from imgagent.http import HttpClient, HttpConfig
from imgagent.manifest import get_manifest, write_manifest
from imgagent.models import DownloadResult, ImageCandidate
from imgagent.ranking import mark_near_duplicates, rank_candidates, select_diverse
from imgagent.security import SecurityOptions
from imgagent.sources import build_source
from imgagent.sources.base import SearchOptions
from imgagent.util import new_job_id
from imgagent.workspace import Workspace


def make_http(cfg: Config) -> HttpClient:
    return HttpClient(HttpConfig(
        security=SecurityOptions(
            allow_private_addr=cfg.allow_private_addr,
            allow_metadata=cfg.allow_metadata,
            verify_tls=cfg.verify_tls,
            max_redirects=cfg.max_redirects,
        ),
        connect_timeout=cfg.connect_timeout,
        read_timeout=cfg.read_timeout,
        retries=cfg.retries,
        backoff_base=cfg.backoff_base,
        cache_dir=cfg.cache_ttl > 0 and Workspace.from_config(cfg).ensure().cache_dir or None,
        cache_ttl=cfg.cache_ttl,
    ))


class Orchestrator:
    def __init__(self, cfg: Config, *, http: HttpClient | None = None, db: Database | None = None,
                 ws: Workspace | None = None):
        self.cfg = cfg
        self.ws = ws or Workspace.from_config(cfg).ensure()
        self.ws.check_writable()
        self.http = http or make_http(cfg)
        self.db = db or Database(self.ws.db_path)
        self.db.migrate()

    def close(self) -> None:
        self.http.close()
        self.db.close()

    # -- search -----------------------------------------------------------------
    def search_only(self, query: str, *, limit: int = 20, sources: list[str] | None = None,
                    min_width: int | None = None, min_height: int | None = None,
                    max_bytes: int | None = None, require_reuse_rights: bool | None = None,
                    prefer_authentic: bool | None = None) -> tuple[str, list[ImageCandidate], list[dict]]:
        """Create a job, search sources, store ranked candidates. Returns (job_id, candidates, errors)."""
        require = self.cfg.require_reuse_rights if require_reuse_rights is None else require_reuse_rights
        prefer = self.cfg.prefer_authentic if prefer_authentic is None else prefer_authentic
        use_sources = sources or self.cfg.sources
        if not use_sources:
            raise UsageError("no sources selected — pass --sources or enable sources in config.toml")
        # Fail fast on unknown source names: usage error, not a silent per-source failure.
        for name in use_sources:
            build_source(name, self.http, self.cfg)

        job_id = new_job_id()
        params = {"limit": limit, "sources": use_sources, "min_width": min_width,
                  "min_height": min_height, "max_bytes": max_bytes,
                  "require_reuse_rights": require, "prefer_authentic": prefer}
        self.db.create_job(job_id, query, params)
        self.db.update_job(job_id, status="searching")

        raw: list[ImageCandidate] = []
        errors: list[dict] = []
        for name in use_sources:
            try:
                src = build_source(name, self.http, self.cfg)
                found = src.search(SearchOptions(
                    query=query, limit=limit, min_width=min_width, min_height=min_height,
                    max_bytes=max_bytes, require_reuse_rights=require, prefer_authentic=prefer))
                raw.extend(found)
                self.db.log_source_event(job_id, name, "search_ok",
                                         {"count": len(found), "best_effort": src.capabilities().best_effort})
            except NotImplementedError as exc:
                errors.append({"source": name, "kind": "unsupported", "error": str(exc)})
                self.db.log_source_event(job_id, name, "source_error", {"error": str(exc)})
            except ProviderError as exc:
                errors.append({"source": name, "kind": "provider_error", "error": str(exc)})
                self.db.log_source_event(job_id, name, "source_error", {"error": str(exc)})
            except Exception as exc:
                errors.append({"source": name, "kind": "unexpected", "error": str(exc)})
                self.db.log_source_event(job_id, name, "source_error", {"error": str(exc)})

        ranked = rank_candidates(raw, query, prefer_authentic=prefer, require_reuse_rights=require)
        mark_near_duplicates(ranked)
        if ranked:
            self.db.upsert_candidates(job_id, ranked)
        status = "ranked" if ranked else "empty"
        summary = {"candidates": len(ranked), "sources_ok": len(use_sources) - len(errors),
                   "sources_failed": len(errors)}
        self.db.update_job(job_id, status=status, summary=summary)
        return job_id, ranked, errors

    # -- download -----------------------------------------------------------------
    def download_from_job(self, job_id: str, *, top: int | None = None, ids: list[str] | None = None,
                          project: str | None = None, scene: str | None = None,
                          out_root: str | None = None) -> dict[str, Any]:
        """Download selected candidates (by ids, or top-N ranked) and update the manifest."""
        job = self.db.resolve_job_ref(job_id)
        project = project or self.cfg.default_project
        if out_root:
            from pathlib import Path
            self.cfg.assets_root = Path(out_root).expanduser()
            self.ws.assets_root = self.cfg.assets_root

        if ids:
            candidates = self.db.get_candidates_by_ids(ids)
        else:
            candidates, _ = self.db.list_candidates(job.id, limit=top or 5)
        if not candidates:
            raise NotFoundError("no candidates match the download selection")

        opts = DownloadOptions(
            project=project, scene=scene, job_id=job.id, concurrency=self.cfg.concurrency,
            max_bytes=self.cfg.max_bytes, max_pixels=self.cfg.max_pixels,
            thumbnails=self.cfg.thumbnails, thumb_size=self.cfg.thumb_size,
            security=SecurityOptions(allow_private_addr=self.cfg.allow_private_addr,
                                     allow_metadata=self.cfg.allow_metadata,
                                     verify_tls=self.cfg.verify_tls,
                                     max_redirects=self.cfg.max_redirects),
        )
        dl = Downloader(self.http, self.ws, self.db)
        self.db.update_job(job.id, status="downloading")
        results = dl.download_many(candidates, opts)
        summary = summarize_downloads(results)
        summary["candidates_selected"] = len(candidates)
        partial = 0 < summary["downloaded"] + summary["duplicate"] + summary["skipped_exists"] < len(candidates)
        status = "complete" if summary["failed"] == 0 else ("partial" if summary["downloaded"] else "failed")
        self.db.update_job(job.id, status=status, summary=summary)

        manifest = get_manifest(self.db, job.id)
        manifest_path = write_manifest(self.ws.manifests_dir, manifest)
        return {"job_id": job.id, "results": results, "summary": summary,
                "partial": partial, "manifest_path": str(manifest_path), "project": project}

    # -- full run --------------------------------------------------------------------
    def run(self, query: str, *, limit: int = 20, top: int = 5, sources: list[str] | None = None,
            min_width: int | None = None, min_height: int | None = None, max_bytes: int | None = None,
            require_reuse_rights: bool | None = None, prefer_authentic: bool | None = None,
            project: str | None = None, scene: str | None = None, out_root: str | None = None,
            diversity: bool = True) -> dict[str, Any]:
        job_id, ranked, errors = self.search_only(
            query, limit=limit, sources=sources, min_width=min_width, min_height=min_height,
            max_bytes=max_bytes, require_reuse_rights=require_reuse_rights,
            prefer_authentic=prefer_authentic)
        if not ranked:
            return {"job_id": job_id, "candidates": [], "downloads": None, "errors": errors,
                    "manifest_path": None, "partial": False}

        selection = select_diverse(ranked, top, threshold=self.cfg.diversity_threshold) if diversity else ranked[:top]
        dl_out = self.download_from_job(job_id, ids=[c.candidate_id for c in selection],
                                        project=project, scene=scene, out_root=out_root)
        return {
            "job_id": job_id,
            "candidates": [c.to_dict() for c in ranked],
            "downloads": dl_out["summary"],
            "download_results": [r.to_dict() for r in dl_out["results"]],
            "errors": errors,
            "manifest_path": dl_out["manifest_path"],
            "partial": dl_out["partial"],
        }

    # -- status ----------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        counts = self.db.counts()
        storage = self.db.storage_usage()
        cache_files = list(self.ws.cache_dir.glob("*.json")) if self.ws.cache_dir.is_dir() else []
        return {
            "workspace": str(self.ws.root),
            "assets_root": str(self.ws.assets_root),
            "counts": counts,
            "storage": {**storage, "cache_files": len(cache_files)},
            "sources_enabled": self.cfg.sources,
        }


def detect_orphans(ws: Workspace, db: Database) -> list[str]:
    """Files under any originals/ tree not referenced by the database."""
    known = db.all_asset_paths()
    orphans = []
    for originals in ws.assets_root.glob("*/originals"):
        for f in originals.iterdir():
            if f.is_file() and str(f.resolve()) not in known:
                orphans.append(str(f.resolve()))
    return orphans


__all__ = ["Orchestrator", "make_http", "detect_orphans", "DownloadResult", "C"]
