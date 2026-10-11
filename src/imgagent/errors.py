"""Explicit error taxonomy. Every error maps to a documented exit code."""

from __future__ import annotations

from imgagent import constants as C


class ImgagentError(Exception):
    """Base class. exit_code 1 — unsuccessful operation."""

    exit_code = C.EXIT_NO_RESULTS
    kind = "error"

    def __init__(self, message: str, *, detail: dict | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


class UsageError(ImgagentError):
    """Invalid input or usage. Exit code 2."""

    exit_code = C.EXIT_USAGE
    kind = "usage_error"


class ConfigError(ImgagentError):
    """Configuration problem. Exit code 3."""

    exit_code = C.EXIT_CONFIG
    kind = "config_error"


class DependencyError(ImgagentError):
    """Missing optional dependency or decoder. Exit code 3."""

    exit_code = C.EXIT_CONFIG
    kind = "dependency_error"


class ProviderError(ImgagentError):
    """A source adapter failed. Individual providers failing must not break others."""

    kind = "provider_error"


class SecurityError(ImgagentError):
    """A URL, redirect target or response violated the security policy."""

    kind = "security_error"


class DownloadError(ImgagentError):
    """Network/transport failure while downloading (after retries exhausted)."""

    kind = "download_error"


class ValidationFailed(ImgagentError):
    """Downloaded bytes were rejected: wrong type, corrupt, empty or oversized."""

    kind = "validation_failed"


class NotFoundError(ImgagentError):
    """Requested job / candidate / asset does not exist."""

    kind = "not_found"
