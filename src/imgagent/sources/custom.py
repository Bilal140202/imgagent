"""Config-driven adapter for official search APIs ("bring your own endpoint").

Defined in config.toml:

    [sources.custom.unsplash]
    endpoint = "https://api.unsplash.com/search/photos"
    method = "GET"
    params  = { query = "{query}", per_page = "{limit}" }
    headers = { Authorization = "Client-ID ${UNSPLASH_ACCESS_KEY}" }
    results_path = "results"
    fields = { image_url = "urls.raw", title = "description|alt_description",
               width = "width", height = "height", source_page_url = "links.html" }
    rights_status = "permission_required"

Placeholders: {query} (URL-encoded), {limit}, {offset}. `${VAR}` inside header
values expands from the environment (keys stay out of config files and logs).
`fields` maps candidate attributes to dot-paths in each result item; `|` lists
fallbacks. Unresolvable values stay null — nothing is invented.
"""

from __future__ import annotations

import os
import re
from urllib.parse import quote_plus

from imgagent import constants as C
from imgagent.errors import ProviderError
from imgagent.models import ImageCandidate
from imgagent.sources.base import ImageSource, SearchOptions, SourceCapabilities
from imgagent.util import now_utc_iso

_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")

_CANDIDATE_KEYS = ("image_url", "thumbnail_url", "source_page_url", "title", "description",
                   "creator", "license_name", "license_url", "width", "height",
                   "byte_size", "format")


def _lookup(item: dict, path: str):
    """Dot-path lookup with '|' fallbacks; returns None when nothing resolves."""
    for part in path.split("|"):
        cur = item
        ok = True
        for seg in part.strip().split("."):
            if isinstance(cur, dict) and seg in cur:
                cur = cur[seg]
            else:
                ok = False
                break
        if ok and cur is not None:
            return cur
    return None


class CustomApiSource(ImageSource):
    name = "custom"

    def __init__(self, http, spec):
        super().__init__(http, spec)
        self.spec = spec

    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(
            name=f"custom:{self.spec.name}",
            description=f"Config-driven official API adapter for {self.spec.endpoint}",
            requires_api_key=bool(self.spec.headers),
            rate_limit="depends on the configured API",
            notes=self.spec.notes or "Endpoints and field mappings are declared in config.toml.",
        )

    def _render(self, value: str, options: SearchOptions, offset: int) -> str:
        value = value.replace("{query}", quote_plus(options.query))
        value = value.replace("{limit}", str(options.limit))
        value = value.replace("{offset}", str(offset))
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)

    def search(self, options: SearchOptions) -> list[ImageCandidate]:
        endpoint = self._render(self.spec.endpoint, options, 0)
        params = {k: self._render(v, options, 0) for k, v in self.spec.params.items()}
        headers = {k: self._render(v, options, 0) for k, v in self.spec.headers.items()}
        try:
            data = self.http.get_json(endpoint, params=params, headers=headers or None,
                                      purpose=f"custom.{self.spec.name}", use_cache=False)
        except Exception as exc:
            raise ProviderError(f"custom source {self.spec.name!r} failed: {exc}") from exc
        results = data
        for seg in self.spec.results_path.split("."):
            if isinstance(results, dict) and seg in results:
                results = results[seg]
            else:
                results = None
                break
        if results is None:
            raise ProviderError(
                f"custom source {self.spec.name!r}: results_path {self.spec.results_path!r} not found in response")
        if not isinstance(results, list):
            results = [results]
        out = []
        for item in results[: options.limit]:
            if not isinstance(item, dict):
                continue
            image_url = _lookup(item, self.spec.fields.get("image_url", "url"))
            if not image_url or not isinstance(image_url, str) or not image_url.startswith(("http://", "https://")):
                continue
            values = {k: _lookup(item, self.spec.fields[k]) for k in _CANDIDATE_KEYS
                      if k in self.spec.fields and k != "image_url"}
            out.append(ImageCandidate(
                provider=f"custom:{self.spec.name}",
                image_url=image_url,
                search_query=options.query,
                title=values.get("title") if isinstance(values.get("title"), str) else None,
                description=values.get("description") if isinstance(values.get("description"), str) else None,
                thumbnail_url=values.get("thumbnail_url") if isinstance(values.get("thumbnail_url"), str) else None,
                source_page_url=values.get("source_page_url") if isinstance(values.get("source_page_url"), str) else None,
                creator=values.get("creator") if isinstance(values.get("creator"), str) else None,
                license_name=values.get("license_name") if isinstance(values.get("license_name"), str) else None,
                license_url=values.get("license_url") if isinstance(values.get("license_url"), str) else None,
                width=int(values["width"]) if str(values.get("width", "")).isdigit() else None,
                height=int(values["height"]) if str(values.get("height", "")).isdigit() else None,
                byte_size=int(values["byte_size"]) if str(values.get("byte_size", "")).isdigit() else None,
                format=values.get("format") if values.get("format") in C.ALLOWED_FORMATS else None,
                retrieved_at=now_utc_iso(),
                rights_status=self.spec.rights_status,
            ))
        return out

    def health_check(self) -> bool:
        return True
