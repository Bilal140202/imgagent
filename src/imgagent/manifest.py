"""Manifest builder/exporter — the stable contract between imgagent and consumers.

The manifest is what Opuscar Studio (or any other agent) reads: stable asset
ids, verified local paths, scene assignment, source URLs, attribution, rights
status and file hashes. Consumers never need to know how an image was found.
"""

from __future__ import annotations

import json
from pathlib import Path

from imgagent import constants as C
from imgagent.db import Database
from imgagent.models import Asset, ImageCandidate, Job
from imgagent.util import now_utc_iso


def asset_record(asset: Asset) -> dict:
    return {
        "asset_id": asset.asset_id,
        "candidate_id": asset.candidate_id,
        "job_id": asset.job_id,
        "project": asset.project,
        "scene": asset.scene,
        "file": {
            "path": asset.path,
            "sha256": asset.sha256,
            "byte_size": asset.byte_size,
            "width": asset.width,
            "height": asset.height,
            "format": asset.format,
            "thumbnail_path": asset.thumbnail_path,
        },
        "provenance": {
            "provider": asset.provider,
            "source_url": asset.source_url,
            "source_page_url": asset.source_page_url,
            "creator": asset.creator,
            "license_name": asset.license_name,
            "license_url": asset.license_url,
            "attribution": asset.attribution,
        },
        "rights_status": asset.rights_status,
        "authenticity": asset.authenticity,
        "downloaded_at": asset.downloaded_at,
    }


def build_manifest(job: Job, candidates: list[ImageCandidate], assets: list[Asset],
                   *, errors: list[dict] | None = None) -> dict:
    return {
        "schema_version": C.MANIFEST_SCHEMA,
        "manifest_id": f"manifest-{job.id}",
        "job": {
            "id": job.id,
            "query": job.query,
            "status": job.status,
            "params": job.params,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
        },
        "generated_at": now_utc_iso(),
        "counts": {
            "candidates": len(candidates),
            "assets": len(assets),
        },
        "errors": errors or [],
        "assets": [asset_record(a) for a in assets],
    }


def write_manifest(manifests_dir: Path, manifest: dict) -> Path:
    manifests_dir.mkdir(parents=True, exist_ok=True)
    path = manifests_dir / f"{manifest['job']['id']}.json"
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def get_manifest(db: Database, job_id: str) -> dict | None:
    """Rebuild the manifest for a job from current DB state (never re-downloads)."""
    job = db.get_job(job_id)
    if job is None:
        return None
    candidates, _ = db.list_candidates(job_id, limit=10_000)
    assets, _ = db.list_assets(job_id, limit=10_000)
    events = db.get_source_events(job_id)
    errors = [e for e in events if e["event"] in ("source_error", "download_failed")]
    return build_manifest(job, candidates, assets, errors=errors)
