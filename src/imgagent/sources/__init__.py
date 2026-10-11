"""Source registry: built-in adapters + config-defined custom adapters."""

from __future__ import annotations

from imgagent.errors import UsageError
from imgagent.http import HttpClient
from imgagent.sources.base import (  # noqa: F401  (re-exported surface)
    ImageSource,
    InspectResult,
    SearchOptions,
    SourceCapabilities,
    available_sources,
)
from imgagent.sources.custom import CustomApiSource
from imgagent.sources.duckduckgo import DuckDuckGoSource
from imgagent.sources.generic import GenericSource
from imgagent.sources.openverse import OpenverseSource
from imgagent.sources.wikimedia import WikimediaSource

_BUILTIN = {
    "wikimedia": WikimediaSource,
    "openverse": OpenverseSource,
    "duckduckgo": DuckDuckGoSource,
    "generic": GenericSource,
}


def builtin_source_names() -> list[str]:
    return sorted(_BUILTIN)


def build_source(name: str, http: HttpClient, config=None) -> ImageSource:
    """Instantiate a source by name ('wikimedia', 'custom:foo', or a custom key from config)."""
    custom = getattr(config, "custom_sources", {}) if config else {}
    key = name.split(":", 1)[1] if name.startswith("custom:") else name
    if key in custom and (name.startswith("custom:") or key not in _BUILTIN):
        return CustomApiSource(http, custom[key])
    if name.startswith("custom:"):
        raise UsageError(f"custom source {key!r} is not defined in config.toml [sources.custom.{key}]")
    cls = _BUILTIN.get(name)
    if cls is None:
        raise UsageError(
            f"unknown source {name!r}; available: {', '.join(sorted(set(_BUILTIN)))} "
            f"or custom:<name> entries from config.toml"
        )
    src_cfg = getattr(config, "source_configs", {}).get(name) if config else None
    return cls(http, src_cfg)


def build_sources(names: list[str], http: HttpClient, config=None) -> list[ImageSource]:
    return [build_source(n, http, config) for n in names]
