"""Small deterministic helpers: time, ids, hashing, slugs, HTML stripping."""

from __future__ import annotations

import hashlib
import html as html_mod
import re
import unicodedata
import uuid
from datetime import datetime, timezone


def now_utc_iso() -> str:
    """UTC timestamp, second precision, always ending in Z (e.g. 2026-10-12T09:30:00Z)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def new_id(prefix: str) -> str:
    """Random short id with prefix, e.g. `j-3f9c2a1b` (job ids also carry a timestamp)."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def new_job_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"j-{stamp}-{uuid.uuid4().hex[:4]}"


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(text: str | None) -> str | None:
    """Collapse an HTML fragment (MediaWiki extmetadata) to plain text."""
    if text is None:
        return None
    text = _TAG_RE.sub(" ", text)
    text = html_mod.unescape(text)
    return _WS_RE.sub(" ", text).strip() or None


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str | None, max_len: int = 48) -> str:
    """ASCII, lowercase, hyphen-separated. Deterministic and filesystem-safe."""
    if not text:
        return "image"
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = _SLUG_RE.sub("-", text.lower()).strip("-")
    return (text[:max_len].rstrip("-")) or "image"


def parse_bool(raw: str | None, default: bool = False) -> bool:
    """Parse an env-style boolean string; None/empty falls back to default."""
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header (seconds form or HTTP-date is approximated)."""
    if not value:
        return None
    try:
        return max(0.0, min(float(value.strip()), 120.0))
    except ValueError:
        return None


def normalize_url_for_dedup(url: str) -> str:
    """Canonical form used for cross-provider URL dedup: lowercase scheme/host, drop fragment."""
    m = re.match(r"^(https?)://([^/?#]+)([^#]*)(#.*)?$", url.strip(), re.I)
    if not m:
        return url.strip().lower()
    scheme, host, path, _frag = m.groups()
    return f"{scheme.lower()}://{host.lower()}{path}"


def human_bytes(n: int | float | None) -> str:
    if n is None:
        return "-"
    step = 1024.0
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < step:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= step
    return f"{n:.1f}TiB"
