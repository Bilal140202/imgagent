"""ImageSource interface and registry.

Every adapter implements the same contract:
    search(query, options)  -> list[ImageCandidate]
    inspect(url)            -> InspectResult
    health_check()          -> bool
    capabilities()          -> SourceCapabilities

Adapters are independent: a broken provider never breaks the others — the
orchestrator catches per-source exceptions and records structured errors.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from imgagent.http import HttpClient
from imgagent.models import ImageCandidate


@dataclass
class SearchOptions:
    query: str
    limit: int = 20
    min_width: int | None = None
    min_height: int | None = None
    max_bytes: int | None = None
    require_reuse_rights: bool = False
    prefer_authentic: bool = False
    variants: list[str] = field(default_factory=list)  # query variants to also try


@dataclass
class SourceCapabilities:
    name: str
    description: str
    requires_api_key: bool = False
    env_keys: tuple[str, ...] = ()
    rate_limit: str = ""
    formats: tuple[str, ...] = ()
    notes: str = ""
    best_effort: bool = False     # unofficial / volatile endpoint
    supports_search: bool = True
    supports_inspect: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "description": self.description,
            "requires_api_key": self.requires_api_key, "env_keys": list(self.env_keys),
            "rate_limit": self.rate_limit, "formats": list(self.formats),
            "notes": self.notes, "best_effort": self.best_effort,
            "supports_search": self.supports_search, "supports_inspect": self.supports_inspect,
        }


@dataclass
class InspectResult:
    """Outcome of URL inspection without persisting contents."""

    kind: str  # "image" | "page"
    url: str
    candidate: ImageCandidate | None = None   # for direct image URLs
    page_candidates: list[ImageCandidate] = field(default_factory=list)  # for pages
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "url": self.url,
            "candidate": self.candidate.to_dict() if self.candidate else None,
            "page_candidates": [c.to_dict() for c in self.page_candidates],
            "notes": self.notes,
        }


class ImageSource(ABC):
    name = "base"

    def __init__(self, http: HttpClient, config: Any = None):
        self.http = http
        self.config = config

    @abstractmethod
    def search(self, options: SearchOptions) -> list[ImageCandidate]:
        """Return normalized candidates. Raise ProviderError on total failure."""

    def inspect(self, url: str) -> InspectResult:
        raise NotImplementedError(f"source {self.name!r} cannot inspect URLs")

    def health_check(self) -> bool:
        try:
            self.search(SearchOptions(query="test", limit=1))
            return True
        except Exception:
            return False

    @abstractmethod
    def capabilities(self) -> SourceCapabilities:
        ...


# --- registry -------------------------------------------------------------------
_FACTORIES: dict[str, Callable] = {}


def register(name: str):
    def deco(fn):
        _FACTORIES[name] = fn
        return fn
    return deco


def available_sources() -> list[str]:
    """Source names known to the system (built-ins are registered in sources/__init__)."""
    from imgagent.sources import builtin_source_names
    return builtin_source_names()


def get_source(name: str, http: HttpClient, config: Any = None) -> ImageSource:
    """Build a source by name. Unknown names raise ValueError (caller → UsageError)."""
    factory = _FACTORIES.get(name)
    if factory is None:
        raise ValueError(f"unknown source {name!r}; available: {', '.join(available_sources())}")
    return factory(http, config)
