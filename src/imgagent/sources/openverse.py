"""Openverse (openverse.org) — official CC-focused API, anonymous access allowed.

Endpoint: https://api.openverse.org/v1/images (no key required for anonymous,
rate-limited access; set OPENVERSE_API_KEY for higher limits — read from env
only, never logged).
"""

from __future__ import annotations

import os
import re

from imgagent import constants as C
from imgagent.errors import ProviderError
from imgagent.models import ImageCandidate
from imgagent.sources.base import ImageSource, SearchOptions, SourceCapabilities
from imgagent.util import now_utc_iso

DEFAULT_API_URL = "https://api.openverse.org/v1/images"

_MIME_TO_FORMAT = {"image/jpeg": "JPEG", "image/png": "PNG", "image/gif": "GIF", "image/webp": "WEBP"}

# license codes: cc0/pdm -> PD; by* -> verified license; nc/nd flagged in extra
_PD_CODES = {"cc0", "pdm"}


def map_openverse_license(license_code: str | None) -> tuple[str, list[str]]:
    low = (license_code or "").lower()
    restrictions = [t for t in ("nc", "nd") if t in low]
    if low in _PD_CODES:
        return C.RIGHTS_VERIFIED_PD, []
    if low.startswith(("by", "cc-by")) or low:
        # any other declared license is treated as a verified license with the
        # license name preserved; nc/nd restrictions recorded for policy filters
        return C.RIGHTS_VERIFIED_LICENSE, restrictions
    return C.RIGHTS_UNKNOWN, []


class OpenverseSource(ImageSource):
    name = "openverse"

    def _api_url(self) -> str:
        if self.config is not None:
            api = getattr(self.config, "api_url", None)
            if api:
                return api
        return DEFAULT_API_URL

    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(
            name=self.name,
            description="Openverse aggregated search over CC-licensed and public-domain images "
                        "(Flickr, Wikimedia, museums and more) via the official API.",
            requires_api_key=False,
            env_keys=("OPENVERSE_API_KEY",),
            rate_limit="anonymous ~100 req/day, 5 req/min; authenticated higher",
            formats=("JPEG", "PNG", "GIF", "WEBP"),
            notes="Strongest source for rights-cleared reuse: every result carries a declared license.",
        )

    def _headers(self) -> dict | None:
        key = os.environ.get("OPENVERSE_API_KEY")
        return {"Authorization": f"Bearer {key}"} if key else None

    def search(self, options: SearchOptions) -> list[ImageCandidate]:
        query = options.query.strip()
        if not query:
            return []
        page_size = max(1, min(options.limit, 20))  # anonymous tier caps page_size at 20
        params = {"q": query, "page_size": str(page_size)}
        if options.min_width:
            params["size"] = "large"  # coarse filter; precise checks stay client-side
        try:
            data = self.http.get_json(self._api_url(), params=params, headers=self._headers(),
                                      purpose="openverse.search")
        except Exception as exc:
            raise ProviderError(f"openverse search failed: {exc}") from exc
        out = []
        for item in (data.get("results") or [])[: options.limit]:
            cand = self._normalize(item, query)
            if cand is None:
                continue
            if options.min_width and (cand.width or 0) < options.min_width:
                continue
            if options.min_height and (cand.height or 0) < options.min_height:
                continue
            out.append(cand)
        return out

    def _normalize(self, item: dict, query: str) -> ImageCandidate | None:
        image_url = item.get("url")
        if not image_url or not re.match(r"^https?://", image_url, re.I):
            return None
        license_code = item.get("license")
        rights, restrictions = map_openverse_license(license_code)
        license_version = item.get("license_version")
        license_name = None
        if license_code:
            code_upper = license_code.upper().replace("CC-", "CC ")
            name = code_upper if code_upper.startswith("CC") else f"CC {code_upper}"
            license_name = f"{name} {license_version}".strip() if license_version else name
        fmt = _MIME_TO_FORMAT.get(item.get("filetype") and f"image/{item['filetype']}", None)
        return ImageCandidate(
            provider=self.name,
            image_url=image_url,
            search_query=query,
            title=item.get("title") or None,
            description=None,
            thumbnail_url=item.get("thumbnail"),
            source_page_url=item.get("foreign_landing_url"),
            creator=item.get("creator") or None,
            license_name=license_name,
            license_url=item.get("license_url") or None,
            attribution=(f"{item.get('creator') or 'unknown'} — {license_name or license_code} — via Openverse"
                         if license_code else None),
            width=item.get("width"),
            height=item.get("height"),
            format=fmt,
            byte_size=item.get("filesize"),
            retrieved_at=now_utc_iso(),
            rights_status=rights,
            extra={"license_code": license_code, "license_restrictions": restrictions,
                   "source_flavor": item.get("source")},
        )

    def health_check(self) -> bool:
        try:
            self.http.get_json(self._api_url(), params={"q": "test", "page_size": "1"},
                               headers=self._headers(), purpose="openverse.health", use_cache=False)
            return True
        except Exception:
            return False
