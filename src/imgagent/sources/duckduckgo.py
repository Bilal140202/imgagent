"""DuckDuckGo image discovery — best-effort, unofficial endpoint, no API key.

Uses the community vqd-token flow (search page -> /i.js JSON). This is an
UNOFFICIAL surface: it may change or throttle without notice, so the adapter is
marked best_effort and raises a graceful ProviderError — the orchestrator
continues with the remaining sources.

No CAPTCHA, paywall or access-control bypassing is performed: when DDG serves a
block page, we report failure honestly.
"""

from __future__ import annotations

import re

from imgagent import constants as C
from imgagent.errors import DownloadError, ProviderError
from imgagent.models import ImageCandidate
from imgagent.security import validate_url
from imgagent.sources.base import ImageSource, SearchOptions, SourceCapabilities
from imgagent.util import now_utc_iso

_WEB_VQD_RE = re.compile(r'vqd="?([\d-]+)"?')


class DuckDuckGoSource(ImageSource):
    name = "duckduckgo"

    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(
            name=self.name,
            description="DuckDuckGo Images discovery via the community vqd flow (unofficial, keyless).",
            requires_api_key=False,
            rate_limit="unofficial; keep volumes low, expect throttling",
            formats=("JPEG", "PNG", "GIF", "WEBP"),
            best_effort=True,
            notes="Results carry no license metadata (rights_status stays unknown). "
                  "Endpoint volatility is expected; other sources are unaffected.",
        )

    def search(self, options: SearchOptions) -> list[ImageCandidate]:
        query = options.query.strip()
        if not query:
            return []
        page_text = self._fetch_text("https://duckduckgo.com/",
                                     params={"q": query, "iax": "images", "ia": "images"})
        m = _WEB_VQD_RE.search(page_text)
        if not m:
            raise ProviderError("duckduckgo: could not obtain vqd token (endpoint may have changed)")
        vqd = m.group(1)
        params = {"l": "us-en", "o": "json", "q": query, "vqd": vqd, "f": ",,,", "p": "1"}
        try:
            data = self.http.get_json("https://duckduckgo.com/i.js", params=params,
                                      headers={"Referer": "https://duckduckgo.com/"},
                                      purpose="duckduckgo.ijs", use_cache=False)
        except Exception as exc:
            raise ProviderError(f"duckduckgo i.js failed: {exc}") from exc
        out = []
        for item in (data.get("results") or [])[: options.limit]:
            image_url = item.get("image")
            if not image_url or not re.match(r"^https?://", image_url, re.I):
                continue
            fmt = (item.get("image_type") or "").upper()
            fmt = fmt if fmt in C.ALLOWED_FORMATS else None
            out.append(ImageCandidate(
                provider=self.name,
                image_url=image_url,
                search_query=query,
                title=item.get("title") or None,
                source_page_url=item.get("url") or None,
                thumbnail_url=item.get("thumbnail") or None,
                width=item.get("width") or None,
                height=item.get("height") or None,
                format=fmt,
                retrieved_at=now_utc_iso(),
                rights_status=C.RIGHTS_UNKNOWN,
                extra={"source_site": item.get("source")},
            ))
        return out

    def _fetch_text(self, url: str, params: dict) -> str:
        """GET returning raw text (the vqd token lives in the HTML page)."""
        try:
            validated = validate_url(url, self.http.security, purpose="duckduckgo.page")
            resp = self.http._client.get(validated, params=params)
            if resp.status_code >= 400:
                raise ProviderError(f"duckduckgo page returned HTTP {resp.status_code}")
            return resp.text
        except ProviderError:
            raise
        except Exception as exc:
            raise DownloadError(f"duckduckgo page fetch failed: {exc}") from exc

    def health_check(self) -> bool:
        try:
            return bool(self.search(SearchOptions(query="test", limit=1)))
        except Exception:
            return False
