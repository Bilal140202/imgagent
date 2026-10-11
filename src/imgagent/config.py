"""Configuration: defaults < config.toml < environment < CLI overrides.

Env vars:
  IMGAGENT_WORKSPACE, IMGAGENT_ASSETS_ROOT, IMGAGENT_SOURCES,
  IMGAGENT_CONCURRENCY, IMGAGENT_RETRIES, IMGAGENT_READ_TIMEOUT,
  IMGAGENT_MAX_BYTES, IMGAGENT_CACHE_TTL, IMGAGENT_ALLOW_PRIVATE,
  IMGAGENT_ALLOW_METADATA, IMGAGENT_VERIFY_TLS,
  IMGAGENT_SOURCE_<NAME>_API_URL   (per-provider endpoint override, e.g.
                                    IMGAGENT_WIKIMEDIA_API_URL)
API keys are read from provider-specific env vars only (e.g. OPENVERSE_API_KEY,
UNSPLASH_ACCESS_KEY) and are never logged or persisted by imgagent.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from imgagent import constants as C
from imgagent.errors import ConfigError
from imgagent.util import parse_bool

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on 3.10 only
    import tomli as tomllib  # type: ignore[no-redef]

DEFAULT_SOURCES = ["wikimedia", "openverse", "duckduckgo"]

_TRUTHY = ("1", "true", "yes", "on")


@dataclass
class SourceConfig:
    name: str
    api_url: str | None = None
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class CustomSourceConfig:
    name: str
    endpoint: str
    method: str = "GET"
    params: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    results_path: str = "results"
    fields: dict[str, str] = field(default_factory=dict)
    rights_status: str = C.RIGHTS_UNKNOWN
    notes: str = ""


@dataclass
class Config:
    workspace: Path
    assets_root: Path
    sources: list[str] = field(default_factory=lambda: list(DEFAULT_SOURCES))
    source_configs: dict[str, SourceConfig] = field(default_factory=dict)
    custom_sources: dict[str, CustomSourceConfig] = field(default_factory=dict)

    # HTTP / download behaviour
    concurrency: int = 4
    retries: int = 3
    backoff_base: float = 1.5
    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    max_bytes: int = C.DEFAULT_MAX_BYTES
    max_redirects: int = C.MAX_REDIRECTS_DEFAULT
    verify_tls: bool = True
    allow_private_addr: bool = False
    allow_metadata: bool = False
    cache_ttl: int = 900

    # Assets
    default_project: str = "default"
    thumbnails: bool = True
    thumb_size: int = 256
    max_pixels: int = C.MAX_PIXELS_DEFAULT

    # Ranking
    prefer_authentic: bool = False
    require_reuse_rights: bool = False
    diversity_threshold: int = 6

    user_agent: str = ""


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in _TRUTHY


def read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception as exc:  # malformed toml is a hard config error
        raise ConfigError(f"cannot parse config file {path}: {exc}") from exc


def load_config(
    workspace_path: Path | None = None,
    *,
    cli_overrides: dict[str, Any] | None = None,
    create: bool = True,
) -> Config:
    """Resolve configuration from defaults, toml, environment and CLI flags."""
    cli = cli_overrides or {}

    ws_raw = (
        (Path(str(workspace_path)).expanduser() if workspace_path else None)
        or cli.get("workspace")
        or _env("IMGAGENT_WORKSPACE")
        or None
    )
    if ws_raw is None:
        ws = Path.cwd() / ".imgagent"
    else:
        ws = Path(str(ws_raw)).expanduser()
    if create:
        ws.mkdir(parents=True, exist_ok=True)

    toml = read_toml(ws / "config.toml") if (ws / "config.toml").is_file() else {}
    http_t = toml.get("http", {}) or {}
    assets_t = toml.get("assets", {}) or {}
    rank_t = toml.get("ranking", {}) or {}
    sources_t = toml.get("sources", {}) or {}

    # --- sources -------------------------------------------------------------
    enabled = cli.get("sources") or _env("IMGAGENT_SOURCES")
    if enabled:
        sources = [s.strip() for s in str(enabled).split(",") if s.strip()]
    else:
        sources = list(sources_t.get("enabled", DEFAULT_SOURCES))

    source_configs: dict[str, SourceConfig] = {}
    for name in sources:
        base = sources_t.get(name, {}) or {}
        if not isinstance(base, dict):
            base = {}
        api_url = base.get("api_url")
        env_url = _env(f"IMGAGENT_SOURCE_{name.upper()}_API_URL") or _env(f"IMGAGENT_{name.upper()}_API_URL")
        if env_url:
            api_url = env_url
        source_configs[name] = SourceConfig(name=name, api_url=api_url, options=dict(base))

    custom_sources: dict[str, CustomSourceConfig] = {}
    for name, block in (sources_t.get("custom", {}) or {}).items():
        if not isinstance(block, dict) or not block.get("endpoint"):
            raise ConfigError(f"[sources.custom.{name}] must define 'endpoint'")
        custom_sources[name] = CustomSourceConfig(
            name=name,
            endpoint=str(block["endpoint"]),
            method=str(block.get("method", "GET")).upper(),
            params={str(k): str(v) for k, v in (block.get("params", {}) or {}).items()},
            headers={str(k): str(v) for k, v in (block.get("headers", {}) or {}).items()},
            results_path=str(block.get("results_path", "results")),
            fields={str(k): str(v) for k, v in (block.get("fields", {}) or {}).items()},
            rights_status=str(block.get("rights_status", C.RIGHTS_UNKNOWN)),
            notes=str(block.get("notes", "")),
        )
        if name not in sources and (cli.get("sources") or _env("IMGAGENT_SOURCES") or sources_t.get("enabled")):
            pass  # explicit enable list wins; custom sources are addressable by name anyway

    # --- paths ----------------------------------------------------------------
    assets_raw = (
        cli.get("assets_root")
        or _env("IMGAGENT_ASSETS_ROOT")
        or assets_t.get("root")
        or None
    )
    assets_root = Path(str(assets_raw)).expanduser() if assets_raw else ws.parent / "assets"

    cfg = Config(
        workspace=ws,
        assets_root=assets_root,
        sources=sources,
        source_configs=source_configs,
        custom_sources=custom_sources,
        concurrency=int(cli.get("concurrency") or _env("IMGAGENT_CONCURRENCY") or http_t.get("concurrency", 4)),
        retries=int(cli.get("retries") or _env("IMGAGENT_RETRIES") or http_t.get("retries", 3)),
        backoff_base=float(http_t.get("backoff_base", 1.5)),
        connect_timeout=float(http_t.get("connect_timeout", 10.0)),
        read_timeout=float(cli.get("read_timeout") or _env("IMGAGENT_READ_TIMEOUT") or http_t.get("read_timeout", 30.0)),
        max_bytes=int(cli.get("max_bytes") or _env("IMGAGENT_MAX_BYTES") or http_t.get("max_bytes", C.DEFAULT_MAX_BYTES)),
        max_redirects=int(http_t.get("max_redirects", C.MAX_REDIRECTS_DEFAULT)),
        verify_tls=parse_bool(_env("IMGAGENT_VERIFY_TLS"), default=_as_bool(http_t.get("verify_tls", True))),
        allow_private_addr=_as_bool(_env("IMGAGENT_ALLOW_PRIVATE", "0") if _env("IMGAGENT_ALLOW_PRIVATE") else http_t.get("allow_private_addr", False)),
        allow_metadata=_as_bool(_env("IMGAGENT_ALLOW_METADATA", "0")),
        cache_ttl=int(_env("IMGAGENT_CACHE_TTL") or http_t.get("cache_ttl", 900)),
        default_project=str(assets_t.get("default_project", "default")),
        thumbnails=_as_bool(assets_t.get("thumbnails", True)),
        thumb_size=int(assets_t.get("thumb_size", 256)),
        max_pixels=int(assets_t.get("max_pixels", C.MAX_PIXELS_DEFAULT)),
        prefer_authentic=_as_bool(cli.get("prefer_authentic", rank_t.get("prefer_authentic", False))),
        require_reuse_rights=_as_bool(cli.get("require_reuse_rights", rank_t.get("require_reuse_rights", False))),
        diversity_threshold=int(rank_t.get("diversity_threshold", 6)),
    )
    if cfg.concurrency < 1 or cfg.retries < 0:
        raise ConfigError("concurrency must be >= 1 and retries >= 0")
    if cfg.max_bytes < 1024:
        raise ConfigError("max_bytes must be at least 1024")
    return cfg
