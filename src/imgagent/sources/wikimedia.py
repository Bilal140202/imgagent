"""Wikimedia Commons via the official MediaWiki API.

Endpoint: https://commons.wikimedia.org/w/api.php (no API key required).
Policy: be polite — max 50 results per page, max 2 pages per query, identify
with the imgagent User-Agent, honor rate limits with backoff.

Rights metadata comes from extmetadata (LicenseShortName/LicenseUrl/Artist/
UsageTerms) and is mapped to the imgagent rights states. Public-domain and CC0
map to verified_public_domain; CC BY*/by-sa* map to verified_license (notice
requirements recorded in attribution); everything else stays permission_required
or unknown. Nothing is inferred beyond what the API returns.
"""

from __future__ import annotations

import re

from imgagent import constants as C
from imgagent.errors import ProviderError
from imgagent.models import ImageCandidate
from imgagent.sources.base import ImageSource, InspectResult, SearchOptions, SourceCapabilities
from imgagent.util import new_id, now_utc_iso, strip_html

DEFAULT_API_URL = "https://commons.wikimedia.org/w/api.php"
ALLOWED_MIME = {"image/jpeg": "JPEG", "image/png": "PNG", "image/gif": "GIF", "image/webp": "WEBP", "image/avif": "AVIF"}

EXTMETA_FIELDS = "LicenseShortName|LicenseUrl|Artist|Credit|ImageDescription|UsageTerms|DateTimeOriginal|Attribution"

_PD_TOKENS = ("public domain", "pd-old", "pd-old-100", "pd-art", "pd-self", "pd-user", "pd-us",
              "cc0", "copyright expired", "no restrictions")
_CC_RE = re.compile(r"^(cc[- ]|attribution|share ?alike)", re.I)


def map_license(license_name: str | None) -> tuple[str, bool]:
    """-> (rights_status, requires_attribution)."""
    if not license_name:
        return C.RIGHTS_UNKNOWN, False
    low = license_name.lower().strip()
    if any(t in low for t in _PD_TOKENS):
        return C.RIGHTS_VERIFIED_PD, False
    if _CC_RE.match(low):
        return C.RIGHTS_VERIFIED_LICENSE, True  # CC BY* always requires attribution
    return C.RIGHTS_PERMISSION_REQUIRED, False


class WikimediaSource(ImageSource):
    name = "wikimedia"

    def _api_url(self) -> str:
        if self.config is not None:
            api = getattr(self.config, "api_url", None)
            if api:
                return api
        return DEFAULT_API_URL

    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(
            name=self.name,
            description="Wikimedia Commons full-text file search via the official MediaWiki API, "
                        "including original-file URLs and license metadata.",
            requires_api_key=False,
            rate_limit="official API; polite limit 1 req/s, max 5000 queries/day for heavy users",
            formats=("JPEG", "PNG", "GIF", "WEBP", "AVIF"),
            notes="Best coverage for historical/documentary photography with explicit licensing. "
                  "Query is filtered client-side to raster mime types.",
            supports_inspect=True,
        )

    def search(self, options: SearchOptions) -> list[ImageCandidate]:
        query = options.query.strip()
        if not query:
            return []
        want = max(1, min(options.limit, 100))
        out: dict[str, ImageCandidate] = {}
        offset = 0
        pages = 0
        while len(out) < want and pages < 2:  # politeness: hard cap 2 API pages
            params = {
                "action": "query",
                "format": "json",
                "formatversion": "2",
                "generator": "search",
                "gsrsearch": f"{query} filetype:bitmap",
                "gsrnamespace": "6",
                "gsrlimit": str(min(50, want - len(out))),
                "gsroffset": str(offset),
                "prop": "imageinfo",
                "iiprop": "url|size|mime|extmetadata",
                "iiextmetadatafilter": EXTMETA_FIELDS,
            }
            try:
                data = self.http.get_json(self._api_url(), params=params, purpose="wikimedia.search")
            except Exception as exc:
                raise ProviderError(f"wikimedia search failed: {exc}") from exc
            pages += 1
            for page in (data.get("query", {}) or {}).get("pages", []) or []:
                cand = self._normalize(page, query)
                if cand is not None and cand.candidate_id not in out:
                    if self._passes_filters(cand, options):
                        out[cand.candidate_id] = cand
            cont = (data.get("continue", {}) or {}).get("gsroffset")
            if not cont or not (data.get("query", {}) or {}).get("pages"):
                break
            offset = int(cont)
        return list(out.values())[: options.limit]

    def _normalize(self, page: dict, query: str) -> ImageCandidate | None:
        infos = page.get("imageinfo") or []
        if not infos:
            return None
        info = infos[0]
        mime = info.get("mime")
        if mime not in ALLOWED_MIME:
            return None
        image_url = info.get("url")
        if not image_url:
            return None
        title = (page.get("title") or "").removeprefix("File:") or None
        if title:
            title = re.sub(r"\.(jpe?g|png|gif|webp|avif|tiff?)$", "", title, flags=re.I) or title
        ext = info.get("extmetadata", {}) or {}

        def meta(key: str) -> str | None:
            v = (ext.get(key, {}) or {}).get("value")
            return strip_html(v) if v else None

        license_name = meta("LicenseShortName") or meta("UsageTerms")
        rights, requires_attr = map_license(license_name)
        creator = meta("Artist") or meta("Credit")
        attribution = None
        if requires_attr and (license_name or creator):
            attribution = (f"{creator or 'unknown creator'} — {title or 'untitled'} — "
                           f"{license_name or 'license required'} — via Wikimedia Commons")
        return ImageCandidate(
            provider=self.name,
            image_url=image_url,
            search_query=query,
            title=title,
            description=meta("ImageDescription"),
            thumbnail_url=(info.get("thumburl") or None),
            source_page_url=info.get("descriptionurl"),
            creator=creator,
            license_name=license_name,
            license_url=meta("LicenseUrl") or (ext.get("LicenseUrl", {}) or {}).get("value"),
            attribution=attribution,
            width=info.get("width"),
            height=info.get("height"),
            format=ALLOWED_MIME.get(mime),
            byte_size=info.get("size"),
            retrieved_at=now_utc_iso(),
            rights_status=rights,
            extra={"mime": mime, "api": "mediawiki"},
        )

    def _passes_filters(self, cand: ImageCandidate, options: SearchOptions) -> bool:
        if options.min_width and (cand.width or 0) < options.min_width:
            return False
        if options.min_height and (cand.height or 0) < options.min_height:
            return False
        if options.max_bytes and cand.byte_size and cand.byte_size > options.max_bytes:
            return False
        return True

    # -- inspect --------------------------------------------------------------------
    def inspect(self, url: str) -> InspectResult:
        m = re.match(r"^https?://commons\.wikimedia\.org/wiki/File:(.+?)(?:[?#].*)?$", url, re.I)
        if not m:
            return InspectResult(kind="page", url=url,
                                 notes=["not a Wikimedia Commons file page; use the generic source"])
            # (fall through to generic handling at the orchestrator level)
        title = m.group(1)
        params = {
            "action": "query", "format": "json", "formatversion": "2",
            "titles": f"File:{title}",
            "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata",
            "iiextmetadatafilter": EXTMETA_FIELDS,
        }
        data = self.http.get_json(self._api_url(), params=params, purpose="wikimedia.inspect")
        pages = (data.get("query", {}) or {}).get("pages", []) or []
        for page in pages:
            if page.get("missing"):
                continue
            cand = self._normalize(page, query=url)
            if cand:
                return InspectResult(kind="image", url=url, candidate=cand)
        return InspectResult(kind="page", url=url, notes=["file page not found or has no imageinfo"])

    def health_check(self) -> bool:
        try:
            params = {"action": "query", "format": "json", "meta": "siteinfo"}
            self.http.get_json(self._api_url(), params=params, purpose="wikimedia.health", use_cache=False)
            return True
        except Exception:
            return False


def _unused_new_id():  # keep import surface stable without lint noise
    return new_id()
