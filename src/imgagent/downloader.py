"""Reusable download + verification engine.

Guarantees (spec section 7):
- streaming HTTP with connection/read timeouts, bounded retries (exponential
  backoff + jitter), Retry-After support, bounded concurrency;
- maximum byte limits enforced while streaming;
- magic-byte sniff + full Pillow decode (never trust extensions/headers);
- JPEG/PNG/WebP/GIF by default, AVIF when a decoder is installed;
- deterministic safe filenames, SHA-256 content hashing;
- exact-duplicate detection by hash with existing-file reuse;
- atomic `.part` writes followed by os.replace rename;
- structured per-item results; a candidate is `downloaded` only after its bytes
  validate and the final path exists.
"""

from __future__ import annotations

import concurrent.futures as futures
import os
import threading
from dataclasses import dataclass, field

from imgagent import constants as C
from imgagent.db import Database
from imgagent.errors import ImgagentError, ValidationFailed
from imgagent.http import HttpClient
from imgagent.images import make_thumbnail, perceptual_hash, validate_image_file
from imgagent.models import Asset, DownloadResult, ImageCandidate
from imgagent.security import SecurityOptions, sanitize_filename
from imgagent.util import now_utc_iso, sha256_file
from imgagent.workspace import MetadataLog, Workspace


@dataclass
class DownloadOptions:
    project: str = "default"
    scene: str | None = None
    job_id: str | None = None
    concurrency: int = 4
    max_bytes: int = C.DEFAULT_MAX_BYTES
    max_pixels: int = C.MAX_PIXELS_DEFAULT
    thumbnails: bool = True
    thumb_size: int = 256
    security: SecurityOptions = field(default_factory=SecurityOptions)


class Downloader:
    def __init__(self, http: HttpClient, workspace: Workspace, db: Database):
        self.http = http
        self.ws = workspace
        self.db = db
        self._meta_lock = threading.Lock()

    # -- single candidate ---------------------------------------------------------
    def download_one(self, candidate: ImageCandidate, opts: DownloadOptions) -> DownloadResult:
        base = DownloadResult(candidate_id=candidate.candidate_id, provider=candidate.provider, status="failed")
        try:
            return self._attempt(candidate, opts)
        except ImgagentError as exc:
            base.error = str(exc)
            base.error_kind = exc.kind
            self.db.update_candidate_status(candidate.candidate_id, C.DL_FAILED)
            self.db.log_source_event(None, candidate.provider, "download_failed",
                                     {"candidate_id": candidate.candidate_id, "error": str(exc)})
            return base
        except Exception as exc:  # unexpected — still a structured failure
            base.error = f"unexpected: {exc}"
            base.error_kind = "internal_error"
            self.db.update_candidate_status(candidate.candidate_id, C.DL_FAILED)
            return base

    def _attempt(self, candidate: ImageCandidate, opts: DownloadOptions) -> DownloadResult:
        project_dir = self.ws.project_dir(opts.project)
        originals = project_dir / "originals"
        thumbs = project_dir / "thumbnails"
        meta_log = MetadataLog(self.ws.metadata_jsonl(opts.project))

        # Resume / reuse: already have verified content for this candidate?
        if candidate.download_status == C.DL_DOWNLOADED and candidate.extra.get("asset_id"):
            asset = self.db.get_asset(candidate.extra["asset_id"])
            if asset and os.path.exists(asset.path):
                return DownloadResult(
                    candidate_id=candidate.candidate_id, provider=candidate.provider,
                    status="skipped_exists", asset_id=asset.asset_id, path=asset.path,
                    sha256=asset.sha256, byte_size=asset.byte_size,
                    width=asset.width, height=asset.height, format=asset.format,
                )

        # Download to atomic .part
        stem = f"{sanitize_filename(candidate.title, fallback='image')}--{candidate.candidate_id[5:13]}"
        part = originals / f".{stem}.part"
        originals.mkdir(parents=True, exist_ok=True)
        try:
            sha, size, content_type, final_url = self.http.download_stream(
                candidate.image_url, part, max_bytes=opts.max_bytes,
                purpose="download",
            )
        except Exception:
            part.unlink(missing_ok=True)
            raise

        try:
            # Content-type early check only when it contradicts magic bytes later anyway;
            # body sniff is the source of truth.
            info = validate_image_file(part, max_pixels=opts.max_pixels)
        except Exception:
            part.unlink(missing_ok=True)
            raise
        if size == 0:
            part.unlink(missing_ok=True)
            raise ValidationFailed("downloaded payload is empty")

        # Exact duplicate by content hash → reuse existing asset, discard new bytes
        existing = self.db.get_asset_by_hash(sha)
        if existing and os.path.exists(existing.path):
            part.unlink(missing_ok=True)
            self.db.update_candidate_status(candidate.candidate_id, C.DL_DOWNLOADED, existing.asset_id)
            return DownloadResult(
                candidate_id=candidate.candidate_id, provider=candidate.provider, status="duplicate",
                asset_id=existing.asset_id, path=existing.path, sha256=sha, byte_size=existing.byte_size,
                width=existing.width, height=existing.height, format=existing.format,
            )

        ext = C.FORMAT_EXT.get(info.format, ".bin")
        dest = originals / f"{stem}{ext}"
        if dest.exists() and sha256_file(dest) == sha:
            part.unlink(missing_ok=True)  # identical file already on disk
        else:
            os.replace(part, dest)  # atomic rename within same directory

        asset = Asset(
            asset_id=f"asset-{sha[:20]}",
            sha256=sha,
            path=str(dest.resolve()),
            byte_size=size,
            downloaded_at=now_utc_iso(),
            candidate_id=candidate.candidate_id,
            job_id=opts.job_id,
            width=info.width,
            height=info.height,
            format=info.format,
            project=opts.project,
            scene=opts.scene,
            source_url=candidate.image_url,
            source_page_url=candidate.source_page_url,
            provider=candidate.provider,
            creator=candidate.creator,
            license_name=candidate.license_name,
            license_url=candidate.license_url,
            attribution=candidate.attribution,
            rights_status=candidate.rights_status,
            authenticity=candidate.authenticity,
        )

        thumb_path = None
        if opts.thumbnails:
            thumb_path = make_thumbnail(dest, thumbs / f"{dest.stem}.jpg", box=opts.thumb_size)
        asset.thumbnail_path = thumb_path

        # Compute perceptual hash for future near-dup workflows (best effort).
        try:
            asset.extra_phash = perceptual_hash(dest)  # type: ignore[attr-defined]
        except Exception:
            pass

        self.db.record_asset(asset)
        self.db.update_candidate_status(candidate.candidate_id, C.DL_DOWNLOADED, asset.asset_id)
        with self._meta_lock:
            meta_log.append({
                "event": "downloaded",
                **asset.to_dict(),
                "content_type_reported": content_type,
                "final_url": final_url,
            })
        return DownloadResult(
            candidate_id=candidate.candidate_id, provider=candidate.provider, status="downloaded",
            asset_id=asset.asset_id, path=asset.path, sha256=sha, byte_size=size,
            width=info.width, height=info.height, format=info.format,
        )

    # -- batch ---------------------------------------------------------------------
    def download_many(
        self, candidates: list[ImageCandidate], opts: DownloadOptions, *,
        on_result=None,
    ) -> list[DownloadResult]:
        if not candidates:
            return []
        results: list[DownloadResult | None] = [None] * len(candidates)
        index = {c.candidate_id: i for i, c in enumerate(candidates)}

        def work(c: ImageCandidate) -> None:
            res = self.download_one(c, opts)
            results[index[c.candidate_id]] = res
            if on_result:
                on_result(res)

        workers = max(1, min(opts.concurrency, len(candidates)))
        with futures.ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(work, candidates))

        out = [r for r in results if r is not None]
        # Any candidate that never produced a result (should not happen) counts as failed.
        for c in candidates:
            if index[c.candidate_id] < len(results) and results[index[c.candidate_id]] is None:
                out.append(DownloadResult(candidate_id=c.candidate_id, provider=c.provider,
                                          status="failed", error="no result recorded"))
        return out


def summarize_downloads(results: list[DownloadResult]) -> dict:
    return {
        "attempted": len(results),
        "downloaded": sum(1 for r in results if r.status == "downloaded"),
        "duplicate": sum(1 for r in results if r.status == "duplicate"),
        "skipped_exists": sum(1 for r in results if r.status == "skipped_exists"),
        "failed": sum(1 for r in results if r.status == "failed"),
        "errors": [ {"candidate_id": r.candidate_id, "provider": r.provider,
                     "kind": r.error_kind, "error": r.error}
                    for r in results if r.status == "failed" ],
    }
