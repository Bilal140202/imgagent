"""Security policy for outbound URLs and downloaded content.

Treats every search result, page and redirect as untrusted input:

- http/HTTPS only, no credentials embedded in URLs, ports restricted to 80/443
  unless private addressing is explicitly allowed (development mode).
- SSRF protection: the host must resolve to at least one public IP. Loopback,
  private (RFC1918 + CGNAT), link-local, multicast, reserved and unspecified
  addresses are rejected.
- Cloud/instance metadata endpoints (169.254.169.254, fd00:ec2::254) are
  HARD-BLOCKED and cannot be enabled by --dangerously-allow-private; they need
  IMGAGENT_ALLOW_METADATA=1 explicitly (never recommended).
- Redirects are followed manually hop-by-hop; every hop is re-validated.
- Filenames are sanitized against path traversal.

Known limitation (documented in docs/SECURITY.md): DNS is re-resolved by the
HTTP stack at connect time, so a strictly-timed DNS rebinding race cannot be
fully eliminated in-process. Validation happens per request and per redirect
hop, response sizes are capped while streaming, and TLS verification is on by
default. For hostile-network deployments run imgagent inside an egress-restricted
sandbox.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from imgagent.errors import SecurityError
from imgagent.util import slugify

# Instance-metadata endpoints. Blocked unless IMGAGENT_ALLOW_METADATA=1.
METADATA_V4 = ipaddress.ip_address("169.254.169.254")
METADATA_V6 = ipaddress.ip_address("fd00:ec2::254")
METADATA_HOSTS = {"metadata.google.internal", "metadata.goog"}

_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")


@dataclass
class SecurityOptions:
    allow_private_addr: bool = False
    allow_metadata: bool = False
    verify_tls: bool = True
    max_redirects: int = 8
    allowed_ports: tuple[int, ...] = (80, 443)


def resolve_host(host: str) -> list[ipaddress.IPAddress]:
    """Resolve a hostname to all of its addresses (raises SecurityError on failure)."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise SecurityError(f"cannot resolve host {host!r}: {exc}") from exc
    out = []
    for info in infos:
        try:
            out.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    if not out:
        raise SecurityError(f"host {host!r} resolved to no usable addresses")
    return out


def assert_ip_allowed(ip: ipaddress.IPAddress, opts: SecurityOptions) -> None:
    is_metadata = ip in (METADATA_V4, METADATA_V6)
    if is_metadata:
        if opts.allow_metadata:
            return
        raise SecurityError(
            f"refusing to contact cloud/instance metadata address {ip} "
            "(hard-blocked; IMGAGENT_ALLOW_METADATA=1 would override — not recommended)"
        )
    if opts.allow_private_addr:
        return
    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise SecurityError(
            f"refusing to contact private/reserved address {ip} "
            "(set IMGAGENT_ALLOW_PRIVATE=1 only for local testing)"
        )


def validate_url(url: str, opts: SecurityOptions, *, purpose: str = "request") -> str:
    """Validate an outbound URL and return it (normalized). Raises SecurityError."""
    if not url or not isinstance(url, str):
        raise SecurityError(f"empty URL rejected for {purpose}")
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        raise SecurityError(f"only http/https URLs are allowed, got scheme {parts.scheme!r} ({purpose})")
    if not parts.hostname:
        raise SecurityError(f"URL has no hostname: {url!r}")
    if parts.username or parts.password:
        raise SecurityError(f"URLs with embedded credentials are rejected: {url!r}")
    if any(parts.hostname.lower().endswith(s) for s in _BLOCKED_SUFFIXES):
        raise SecurityError(f"host {parts.hostname!r} is blocked ({purpose})")

    port = parts.port
    if port is None:
        port = 443 if parts.scheme.lower() == "https" else 80
    if not opts.allow_private_addr and port not in opts.allowed_ports:
        raise SecurityError(f"port {port} is not allowed for public {purpose}s (allowed: {opts.allowed_ports})")

    try:
        ips = resolve_host(parts.hostname)
    except SecurityError:
        raise
    for ip in ips:
        assert_ip_allowed(ip, opts)
    return url


def validate_redirect(start_url: str, location: str, opts: SecurityOptions, hop: int) -> str:
    """Validate one redirect hop (relative locations resolved against start)."""
    if hop >= opts.max_redirects:
        raise SecurityError(f"too many redirects (>{opts.max_redirects}) while fetching {start_url}")
    target = urljoin(start_url, location)
    return validate_url(target, opts, purpose="redirect")


def sanitize_filename(name: str | None, *, fallback: str = "file", max_len: int = 80) -> str:
    """Path-traversal-proof, cross-platform-safe filename base (no extension)."""
    base = slugify(name, max_len=max_len) if name else ""
    base = base.replace("/", "-").replace("\\", "-")
    base = base.lstrip(".") or fallback
    reserved = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
    stem = base.split(".")[0].lower()
    if stem in reserved:
        base = f"_{base}"
    return base or fallback


def unique_dest(directory: Path, stem: str, ext: str) -> Path:
    """Deterministic destination; caller already embeds a content/url hash for stability."""
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{stem}{ext}"


def check_decompression_bomb(width: int | None, height: int | None, max_pixels: int) -> None:
    if width and height and width * height > max_pixels:
        raise SecurityError(
            f"image {width}x{height} exceeds max_pixels={max_pixels} (decompression-bomb guard)"
        )
