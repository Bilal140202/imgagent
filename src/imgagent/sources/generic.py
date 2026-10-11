"""Generic webpage image extraction — Open Graph, JSON-LD, <img> elements.

Used by `imgagent inspect URL` and as a discovery source for direct page URLs.
Works only on publicly accessible pages; no login, paywall or access-control
bypass. Pinterest pins are handled here as public references: the pin URL is a
PAGE, never an image URL — we resolve its og:image and preserve the pin URL as
source_page_url (best-effort; Pinterest may throttle or block datacenter IPs —
we report failure honestly).
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from urllib.parse import urljoin

from imgagent import constants as C
from imgagent.models import ImageCandidate
from imgagent.security import SecurityError, validate_url
from imgagent.sources.base import ImageSource, InspectResult, SearchOptions, SourceCapabilities
from imgagent.util import now_utc_iso

_MAX_IMGS = 20
_MAX_HTML_BYTES = 3 * 1024 * 1024

_PIN_RE = re.compile(r"^https?://(?:[a-z]{2,3}\.)?pinterest\.[a-z.]+/pin/[\w-]+", re.I)


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.og: dict[str, str] = {}
        self.title: str | None = None
        self.imgs: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._in_title = False
        self._jsonld_raw: list[str] = []
        self._in_jsonld = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            key = a.get("property") or a.get("name")
            if key and a.get("content"):
                self.og[key.lower()] = a["content"]
        elif tag == "img" and a.get("src"):
            self.imgs.append(a["src"])
        elif tag == "link" and a.get("rel") in ("image_src", "preload") and a.get("href"):
            self.links.append((a.get("rel"), a["href"]))
        elif tag == "title":
            self._in_title = True
        elif tag == "script" and "application/ld+json" in (a.get("type") or ""):
            self._in_jsonld = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "script":
            self._in_jsonld = False

    def handle_data(self, data):
        if self._in_title and data.strip():
            self.title = (self.title or "") + data.strip()
        if self._in_jsonld and data.strip():
            self._jsonld_raw.append(data)


def _extract_jsonld_images(raws: list[str]) -> list[str]:
    out: list[str] = []
    for raw in raws:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for d in items:
            if not isinstance(d, dict):
                continue
            img = d.get("image") or d.get("thumbnailUrl") or d.get("thumbnailURL")
            if isinstance(img, str):
                out.append(img)
            elif isinstance(img, list):
                out.extend(i for i in img if isinstance(i, str))
    return out


class GenericSource(ImageSource):
    name = "generic"

    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(
            name=self.name,
            description="Extracts images from publicly accessible webpages: Open Graph tags, "
                        "JSON-LD, and <img> elements. Also resolves public Pinterest pin pages "
                        "to their og:image while preserving the pin URL as the source page.",
            requires_api_key=False,
            formats=("JPEG", "PNG", "GIF", "WEBP"),
            notes="Rights are never inferred: candidates stay rights_status=unknown. "
                  "No login/paywall/CAPTCHA bypass — blocked pages fail explicitly.",
            supports_search=False,
            supports_inspect=True,
        )

    def search(self, options: SearchOptions) -> list[ImageCandidate]:
        raise NotImplementedError("generic source supports URL inspection only — "
                                  "pass a page URL to `imgagent inspect`")

    def health_check(self) -> bool:
        return True  # stateless extractor; nothing to probe

    # -- inspect --------------------------------------------------------------------
    def inspect(self, url: str) -> InspectResult:
        validated = validate_url(url, self.http.security, purpose="generic.inspect")
        try:
            data, content_type, final_url = self.http.fetch_bytes(validated, _MAX_HTML_BYTES,
                                                                  purpose="generic.inspect")
        except SecurityError:
            raise
        except Exception as exc:
            return InspectResult(kind="error", url=url, notes=[f"fetch failed: {exc}"])

        if content_type and content_type.startswith("image/"):
            cand = ImageCandidate(
                provider=self.name,
                image_url=final_url,
                search_query=url,
                source_page_url=None,
                format=content_type.split("/")[1].upper() or None,
                retrieved_at=now_utc_iso(),
                rights_status=C.RIGHTS_UNKNOWN,
            )
            notes = ["URL is itself an image; metadata limited to what headers provide"]
            return InspectResult(kind="image", url=url, candidate=cand, notes=notes)

        html = data.decode("utf-8", errors="replace")
        parser = _PageParser()
        try:
            parser.feed(html)
        except Exception:
            pass  # tolerant parsing: collect whatever we got

        base = final_url
        candidates: list[ImageCandidate] = []
        seen: set[str] = set()

        def add(img_url: str, title: str | None, via: str) -> None:
            absolute = urljoin(base, img_url)
            if not absolute.startswith(("http://", "https://")):
                return
            if absolute in seen or absolute.startswith("data:"):
                return
            seen.add(absolute)
            candidates.append(ImageCandidate(
                provider=self.name,
                image_url=absolute,
                search_query=url,
                title=title or parser.title,
                source_page_url=url,
                width=int(parser.og["og:image:width"]) if parser.og.get("og:image:width", "").isdigit() else None,
                height=int(parser.og["og:image:height"]) if parser.og.get("og:image:height", "").isdigit() else None,
                retrieved_at=now_utc_iso(),
                rights_status=C.RIGHTS_UNKNOWN,
                extra={"via": via},
            ))

        og_image = parser.og.get("og:image")
        if og_image:
            add(og_image, parser.title, "og:image")
        for img in _extract_jsonld_images(parser._jsonld_raw):
            add(img, parser.title, "json-ld")
        for _rel, href in parser.links:
            add(href, parser.title, "link")
        for src in parser.imgs[:_MAX_IMGS]:
            add(src, None, "img")

        notes = []
        if _PIN_RE.match(url):
            notes.append("Pinterest pin resolved as a public reference page; pin URL preserved as "
                         "source_page_url. Automated access may conflict with Pinterest's terms — "
                         "treat results as study references and respect robots.txt/ToS.")
        if not candidates:
            notes.append("no images found on page")
        if content_type and "html" not in content_type:
            notes.append(f"content-type was {content_type}; parsed anyway")
        return InspectResult(kind="page", url=url, page_candidates=candidates[:_MAX_IMGS], notes=notes)
