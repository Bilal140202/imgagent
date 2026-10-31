"""Pinterest pin discovery — best-effort, unofficial surfaces, no API key.

Two anonymous strategies, tried in order:
  A. GET {base}/resource/BaseSearchResource/get/   (unofficial JSON endpoint)
  B. GET {base}/search/pins/?q=…  → parse embedded __PWS_DATA__ /
     __PWS_INITIAL_PROPS__ JSON (server-side-rendered results)

This is an UNOFFICIAL surface. Observed gate variants (all verified live on
2026-10-31 from a datacenter IP): HTTP 403 on strategy A; HTTP 200 with an
empty "results envelope" (resource_response.data as an object, results: [])
on strategy A; and HTTP 200 app-shell pages with no SSR pin data on strategy
B — with or without the isAppShell marker. All of them are reported honestly
via ProviderError; the orchestrator continues with the remaining sources.

No cookies, no login, no CAPTCHA/proxy/TLS-fingerprint bypassing is performed:
when Pinterest blocks us, we say so. Pin images are served anonymously from
i.pinimg.com, so the download stage needs nothing extra. Results carry no
license metadata (rights_status stays unknown — a pin existing proves nothing
about reuse rights).
"""

from __future__ import annotations

import json
import re
from urllib.parse import quote_plus

from imgagent import constants as C
from imgagent.errors import DownloadError, ProviderError, SecurityError
from imgagent.models import ImageCandidate
from imgagent.security import validate_url
from imgagent.sources.base import ImageSource, SearchOptions, SourceCapabilities
from imgagent.util import now_utc_iso

_PINIMG_RE = re.compile(r"^https?://i\.pinimg\.com/", re.I)
_PWS_SCRIPT_RE = re.compile(
    r'<script[^>]*id="(__PWS_DATA__|__PWS_INITIAL_PROPS__)"[^>]*>(.*?)</script>', re.S)
_EXT_FORMAT = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "gif": "GIF", "webp": "WEBP"}

_MAX_PAGE_SIZE = 25          # polite cap for the unofficial endpoint
_MAX_PINS_PARSED = 200       # walker safety cap per response
_MAX_WALK_DEPTH = 14


def _text(value) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class PinterestSource(ImageSource):
    name = "pinterest"

    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(
            name=self.name,
            description="Pinterest pin discovery via unofficial keyless surfaces "
                        "(JSON resource endpoint, SSR __PWS_DATA__ fallback).",
            requires_api_key=False,
            rate_limit="unofficial; keep volumes low, expect 403/empty shells on datacenter IPs",
            formats=("JPEG", "PNG", "GIF", "WEBP"),
            best_effort=True,
            notes="No license metadata exists — rights_status stays unknown, so "
                  "--require-reuse-rights always excludes these candidates. "
                  "Blocked responses and app shells fail gracefully as structured errors.",
        )

    # -- search -------------------------------------------------------------------
    def search(self, options: SearchOptions) -> list[ImageCandidate]:
        query = options.query.strip()
        if not query:
            return []
        base = self._base_url()
        try:
            pins = self._via_resource_endpoint(base, query, options)
            via = "resource_endpoint"
        except Exception as primary_exc:
            try:
                pins, shell = self._via_ssr_html(base, query)
                via = "ssr_html"
            except Exception as fallback_exc:
                raise ProviderError(
                    f"pinterest: resource endpoint failed ({primary_exc}); "
                    f"SSR fallback failed ({fallback_exc})") from fallback_exc
            if not pins:
                # Strategy A already failed (403 / soft gate); an SSR page without
                # pins now means Pinterest is gating us — report it, never fake success.
                detail = ("served an app shell without results (Pinterest gates content by "
                          "IP reputation — datacenter IPs are commonly excluded)" if shell
                          else "contained no pin results (query may have no matches, or "
                               "access is gated by IP reputation)")
                raise ProviderError(
                    f"pinterest: resource endpoint failed ({primary_exc}); "
                    f"SSR page {detail}") from primary_exc
        out: list[ImageCandidate] = []
        seen: set[str] = set()
        for pin in pins:
            if not isinstance(pin, dict):
                continue
            cand = self._normalize(pin, query, via)
            if cand and cand.image_url not in seen:
                seen.add(cand.image_url)
                out.append(cand)
            if len(out) >= max(options.limit, 1):
                break
        return out

    # -- strategy A: unofficial JSON resource endpoint -------------------------------
    def _via_resource_endpoint(self, base: str, query: str,
                               options: SearchOptions) -> list[dict]:
        page_size = min(max(_int_or_none(self._opt("page_size", max(options.limit, 1))) or 25,
                            1), _MAX_PAGE_SIZE)
        payload = json.dumps({
            "options": {"query": query, "scope": "pins",
                        "page_size": page_size, "bookmarks": []},
            "context": {},
        })
        url = (f"{base}/resource/BaseSearchResource/get/"
               f"?source_url={quote_plus('/search/pins/?q=' + query)}&data={quote_plus(payload)}")
        try:
            data = self.http.get_json(url, headers=self._headers(base, query),
                                      purpose="pinterest.search", use_cache=False)
        except Exception as exc:
            raise ProviderError(f"resource endpoint: {exc}") from exc
        if not isinstance(data, dict):
            raise ProviderError("resource endpoint: unexpected non-object response")
        rr = data.get("resource_response")
        if not isinstance(rr, dict):
            raise ProviderError("resource endpoint: missing resource_response object")
        items = rr.get("data")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
        if isinstance(items, dict):
            # Observed soft-gate variant: HTTP 200 + resource_response.data as an
            # object holding (usually empty) results — Pinterest's polite "no".
            results = items.get("results")
            if isinstance(results, list) and results:
                return [item for item in results if isinstance(item, dict)]
            raise ProviderError(
                "resource endpoint: empty results envelope (Pinterest soft gate — "
                "likely IP reputation gating, not a genuine empty index)")
        raise ProviderError("resource endpoint: resource_response.data has an unexpected shape")

    # -- strategy B: SSR JSON embedded in the search page ------------------------------
    def _via_ssr_html(self, base: str, query: str) -> tuple[list[dict], bool]:
        """Returns (pins, shell) where shell=True means a 200 page with no SSR results."""
        html = self._fetch_page(base, query)
        pins: list[dict] = []
        shell = False
        for _sid, body in _PWS_SCRIPT_RE.findall(html):
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            if data.get("isAppShell"):
                shell = True
            props = data.get("props")
            redux = None
            if isinstance(props, dict) and isinstance(props.get("initialReduxState"), dict):
                redux = props["initialReduxState"]
            elif isinstance(data.get("initialReduxState"), dict):
                redux = data["initialReduxState"]
            self._collect_pins(redux if redux is not None else data, pins)
        return pins, shell

    @staticmethod
    def _collect_pins(node, out: list[dict], depth: int = 0) -> None:
        """Location-agnostic walk: find dicts carrying a pin images map (feed shape varies)."""
        if depth > _MAX_WALK_DEPTH or len(out) >= _MAX_PINS_PARSED:
            return
        if isinstance(node, dict):
            images = node.get("images")
            if isinstance(images, dict) and (images.get("orig") or images.get("736x")):
                out.append(node)
                return
            for value in node.values():
                PinterestSource._collect_pins(value, out, depth + 1)
        elif isinstance(node, list):
            for value in node:
                PinterestSource._collect_pins(value, out, depth + 1)

    # -- shared helpers ------------------------------------------------------------
    def _fetch_page(self, base: str, query: str) -> str:
        url = f"{base}/search/pins/?q={quote_plus(query)}"
        try:
            validated = validate_url(url, self.http.security, purpose="pinterest.page")
            resp = self.http._client.get(validated, headers=self._headers(base, query))
        except SecurityError:
            raise
        except Exception as exc:
            raise DownloadError(f"pinterest page fetch failed: {exc}") from exc
        if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location", "?")
            raise ProviderError(
                f"pinterest search page redirected to {location!r} — set "
                "[sources.pinterest] base_url to the resolved regional domain")
        if resp.status_code >= 400:
            raise ProviderError(f"pinterest search page returned HTTP {resp.status_code}")
        return resp.text

    def _headers(self, base: str, query: str) -> dict:
        """Standard browser-like headers (no cookies — anonymous access only)."""
        return {
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "X-Pinterest-PWS-Handler": "www/index.js",
            "Referer": f"{base}/search/pins/?q={quote_plus(query)}",
        }

    def _base_url(self) -> str:
        api_url = getattr(self.config, "api_url", None)
        if api_url:
            return str(api_url).rstrip("/")
        return str(self._opt("base_url", "https://www.pinterest.com")).rstrip("/")

    def _opt(self, key: str, default):
        opts = getattr(self.config, "options", None) or {}
        return opts.get(key, default)

    # -- normalization ----------------------------------------------------------------
    def _normalize(self, pin: dict, query: str, via: str) -> ImageCandidate | None:
        """Map a Pinterest pin object to ImageCandidate. None for anything not a
        usable pin (boards, ads, modules, external/missing image hosts)."""
        images = pin.get("images")
        images = images if isinstance(images, dict) else {}
        best = images.get("orig") or images.get("736x") or {}
        if not isinstance(best, dict):
            best = {}
        image_url = best.get("url")
        if not isinstance(image_url, str) or not _PINIMG_RE.match(image_url):
            return None
        tail = image_url.rsplit("/", 1)[-1]
        fmt = _EXT_FORMAT.get(tail.rsplit(".", 1)[-1].lower()) if "." in tail else None
        if fmt not in C.ALLOWED_FORMATS:
            fmt = None  # downloader magic-byte sniffs instead of trusting extensions
        thumb = (images.get("236x") or images.get("170x") or {})
        thumb_url = thumb.get("url") if isinstance(thumb, dict) else None
        if not isinstance(thumb_url, str) or not _PINIMG_RE.match(thumb_url):
            thumb_url = None
        pin_id = str(pin.get("id")) if pin.get("id") is not None else ""
        extra: dict = {"via": via}
        repins = _int_or_none(pin.get("repin_count"))
        if repins is not None:
            extra["repin_count"] = repins
        link = _text(pin.get("link"))
        if link:
            extra["outer_link"] = link
        board = pin.get("board")
        if isinstance(board, dict) and _text(board.get("name")):
            extra["board"] = _text(board.get("name"))
        pinner = pin.get("pinner")
        if isinstance(pinner, dict) and _text(pinner.get("username")):
            extra["pinner"] = _text(pinner.get("username"))
        return ImageCandidate(
            provider=self.name,
            image_url=image_url,
            search_query=query,
            title=_text(pin.get("title")) or _text(pin.get("grid_title")),
            description=_text(pin.get("description")) or _text(pin.get("auto_alt_text")),
            thumbnail_url=thumb_url,
            source_page_url=f"https://www.pinterest.com/pin/{pin_id}/" if pin_id else None,
            width=_int_or_none(best.get("width")),
            height=_int_or_none(best.get("height")),
            format=fmt,
            retrieved_at=now_utc_iso(),
            rights_status=C.RIGHTS_UNKNOWN,  # never inferred — hard repo rule
            extra=extra,
        )
