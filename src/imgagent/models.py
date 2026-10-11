"""Stable, serializable record models: ImageCandidate, Asset, Job.

These are the contract. `to_dict()` output is versioned via SCHEMA fields in
imgagent.constants. Missing information stays None — never guessed.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any

from imgagent import constants as C
from imgagent.errors import UsageError


def candidate_id_for(provider: str, image_url: str) -> str:
    """Stable id derived from provider + image URL. Same input -> same id."""
    digest = hashlib.sha256(f"{provider}\n{image_url}".encode()).hexdigest()
    return f"cand-{digest[:20]}"


def asset_id_for(sha256_hex: str) -> str:
    return f"asset-{sha256_hex[:20]}"


@dataclass
class ImageCandidate:
    provider: str
    image_url: str
    search_query: str
    candidate_id: str = ""
    title: str | None = None
    description: str | None = None
    thumbnail_url: str | None = None
    source_page_url: str | None = None
    creator: str | None = None
    license_name: str | None = None
    license_url: str | None = None
    attribution: str | None = None
    width: int | None = None
    height: int | None = None
    format: str | None = None
    byte_size: int | None = None
    retrieved_at: str = ""
    relevance_score: float | None = None
    quality_score: float | None = None
    authenticity: str = C.AUTH_UNKNOWN
    rights_status: str = C.RIGHTS_UNKNOWN
    download_status: str = C.DL_NOT_DOWNLOADED
    schema_version: str = C.CANDIDATE_SCHEMA
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.candidate_id:
            self.candidate_id = candidate_id_for(self.provider, self.image_url)
        if not self.retrieved_at:
            from imgagent.util import now_utc_iso
            self.retrieved_at = now_utc_iso()
        if self.rights_status not in C.RIGHTS_STATES:
            self.rights_status = C.RIGHTS_UNKNOWN

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ImageCandidate:
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in data.items() if k in known}
        missing = {"provider", "image_url", "search_query"} - kwargs.keys()
        if missing:
            raise UsageError(f"candidate record missing required fields: {sorted(missing)}")
        return cls(**kwargs)

    def public_dict(self) -> dict[str, Any]:
        """Dict for manifests/JSON output with a stable key order."""
        return {
            "candidate_id": self.candidate_id,
            "provider": self.provider,
            "title": self.title,
            "description": self.description,
            "image_url": self.image_url,
            "thumbnail_url": self.thumbnail_url,
            "source_page_url": self.source_page_url,
            "creator": self.creator,
            "license_name": self.license_name,
            "license_url": self.license_url,
            "attribution": self.attribution,
            "width": self.width,
            "height": self.height,
            "format": self.format,
            "byte_size": self.byte_size,
            "search_query": self.search_query,
            "retrieved_at": self.retrieved_at,
            "relevance_score": self.relevance_score,
            "quality_score": self.quality_score,
            "authenticity": self.authenticity,
            "rights_status": self.rights_status,
            "download_status": self.download_status,
        }


@dataclass
class Asset:
    asset_id: str
    sha256: str
    path: str
    byte_size: int
    downloaded_at: str
    candidate_id: str | None = None
    job_id: str | None = None
    width: int | None = None
    height: int | None = None
    format: str | None = None
    project: str = "default"
    scene: str | None = None
    thumbnail_path: str | None = None
    source_url: str | None = None
    source_page_url: str | None = None
    provider: str | None = None
    creator: str | None = None
    license_name: str | None = None
    license_url: str | None = None
    attribution: str | None = None
    rights_status: str = C.RIGHTS_UNKNOWN
    authenticity: str = C.AUTH_UNKNOWN
    metadata_version: str = C.MANIFEST_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Job:
    id: str
    query: str
    status: str = "created"
    params: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DownloadResult:
    """Outcome of one download attempt (never raises for per-item failures)."""

    candidate_id: str
    provider: str
    status: str  # downloaded | duplicate | skipped_exists | failed
    asset_id: str | None = None
    path: str | None = None
    sha256: str | None = None
    byte_size: int | None = None
    width: int | None = None
    height: int | None = None
    format: str | None = None
    error: str | None = None
    error_kind: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
