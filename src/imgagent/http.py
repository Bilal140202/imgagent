"""Central HTTP layer: one httpx.Client, manual redirect validation, byte caps,
bounded retries with backoff + jitter, Retry-After support, optional JSON cache.

All requests go through validate_url() before being issued and through
validate_redirect() for every hop. Response bodies are consumed as streams and
hard-capped so a hostile server cannot exhaust memory.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

from imgagent import constants as C
from imgagent.__init__ import USER_AGENT
from imgagent.errors import DownloadError, SecurityError
from imgagent.security import SecurityOptions, validate_redirect, validate_url
from imgagent.util import parse_retry_after, sha256_bytes

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


@dataclass
class HttpConfig:
    security: SecurityOptions
    user_agent: str = USER_AGENT
    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    retries: int = 3
    backoff_base: float = 1.5
    max_redirects: int = 8
    cache_dir: Path | None = None
    cache_ttl: int = 900


class HttpCache:
    """Tiny TTL JSON cache stored under <workspace>/cache. Keyed by request identity."""

    def __init__(self, directory: Path, ttl: int):
        self._dir = directory
        self._ttl = ttl
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self._dir / f"{sha256_bytes(key.encode())[:32]}.json"

    def get(self, key: str) -> Any | None:
        if self._ttl <= 0:
            return None
        p = self._path(key)
        if not p.is_file():
            return None
        try:
            blob = json.loads(p.read_text(encoding="utf-8"))
            if time.time() > blob.get("expires", 0):
                return None
            return blob.get("body")
        except Exception:
            return None

    def set(self, key: str, body: Any) -> None:
        if self._ttl <= 0:
            return
        try:
            self._path(key).write_text(
                json.dumps({"expires": time.time() + self._ttl, "body": body}), encoding="utf-8"
            )
        except OSError:
            pass


class HttpClient:
    """All outbound traffic flows through this class."""

    def __init__(self, cfg: HttpConfig):
        self.cfg = cfg
        self.security = cfg.security
        self._client = httpx.Client(
            verify=cfg.security.verify_tls,
            timeout=httpx.Timeout(connect=cfg.connect_timeout, read=cfg.read_timeout, write=30.0, pool=30.0),
            follow_redirects=False,
            headers={"User-Agent": cfg.user_agent, "Accept-Encoding": "gzip, deflate"},
        )
        self._cache = HttpCache(cfg.cache_dir, cfg.cache_ttl) if cfg.cache_dir else None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- internals -------------------------------------------------------------
    def _headers(self, extra: dict | None) -> dict:
        hdrs = dict(self._client.headers)
        if extra:
            hdrs.update({k: v for k, v in extra.items() if v is not None})
        return hdrs

    def _sleep_backoff(self, attempt: int, retry_after: str | None) -> None:
        ra = parse_retry_after(retry_after)
        if ra is not None:
            time.sleep(ra)
        else:
            time.sleep(min(self.cfg.backoff_base * (2 ** attempt) + random.uniform(0, 0.5), 30.0))

    # -- JSON APIs ---------------------------------------------------------------
    def get_json(
        self,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        purpose: str = "api",
        use_cache: bool = True,
    ) -> Any:
        validated = validate_url(url, self.security, purpose=purpose)
        cache_key = None
        if self._cache and use_cache:
            cache_key = f"GET {validated} {json.dumps(params or {}, sort_keys=True)}"
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

        last_error: Exception | None = None
        for attempt in range(max(self.cfg.retries, 0) + 1):
            try:
                url_now = validated
                hop = 0
                while True:
                    resp = self._client.get(url_now, params=params if hop == 0 else None,
                                            headers=self._headers(headers))
                    if resp.is_redirect:
                        url_now = validate_redirect(url_now, resp.headers.get("location", ""),
                                                    self.security, hop)
                        hop += 1
                        continue
                    break
                if resp.status_code in RETRYABLE_STATUS and attempt < self.cfg.retries:
                    last_error = DownloadError(f"{purpose} returned HTTP {resp.status_code}")
                    self._sleep_backoff(attempt, resp.headers.get("retry-after"))
                    continue
                if resp.status_code >= 400:
                    raise DownloadError(f"{purpose} returned HTTP {resp.status_code} for {validated}")
                if len(resp.content) > C.API_RESPONSE_MAX_BYTES:
                    raise SecurityError(
                        f"{purpose} response too large ({len(resp.content)} bytes > {C.API_RESPONSE_MAX_BYTES})"
                    )
                try:
                    body = json.loads(resp.content.decode(resp.encoding or "utf-8", errors="replace"))
                except json.JSONDecodeError as exc:
                    raise DownloadError(f"{purpose} returned non-JSON body: {exc}") from exc
                if self._cache and use_cache and cache_key:
                    self._cache.set(cache_key, body)
                return body
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = DownloadError(f"{purpose} transport failure: {exc}")
                if attempt < self.cfg.retries:
                    self._sleep_backoff(attempt, None)
                    continue
        raise last_error or DownloadError(f"{purpose} failed for {validated}")

    # -- byte fetching (inspect probing) -----------------------------------------
    def fetch_bytes(self, url: str, max_bytes: int, *, headers: dict | None = None,
                    purpose: str = "probe") -> tuple[bytes, str | None, str]:
        """Fetch at most max_bytes. Returns (data, content_type, final_url)."""
        validated = validate_url(url, self.security, purpose=purpose)
        last_error: Exception | None = None
        for attempt in range(max(self.cfg.retries, 0) + 1):
            try:
                cur, hop = validated, 0
                while True:
                    req = self._client.build_request("GET", cur, headers=self._headers(headers))
                    resp = self._client.send(req, stream=True)
                    if resp.is_redirect:
                        cur = validate_redirect(cur, resp.headers.get("location", ""), self.security, hop)
                        hop += 1
                        resp.close()
                        continue
                    break
                try:
                    chunks, total = [], 0
                    for chunk in resp.iter_bytes(65536):
                        total += len(chunk)
                        if total > max_bytes:
                            raise SecurityError(f"probe of {url} exceeded {max_bytes} bytes")
                        chunks.append(chunk)
                    content_type = resp.headers.get("content-type", "").split(";")[0].strip() or None
                    return b"".join(chunks), content_type, str(resp.url)
                finally:
                    resp.close()
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = DownloadError(f"{purpose} transport failure: {exc}")
                if attempt < self.cfg.retries:
                    self._sleep_backoff(attempt, None)
                    continue
        raise last_error or DownloadError(f"{purpose} failed for {url}")

    # -- streaming download ---------------------------------------------------------
    def download_stream(
        self,
        url: str,
        dest_part: Path,
        *,
        max_bytes: int,
        headers: dict | None = None,
        purpose: str = "download",
        on_chunk: Callable[[bytes], None] | None = None,
    ) -> tuple[str, int, str | None, str]:
        """Stream `url` into dest_part with hard byte cap. Returns (sha256, size, content_type, final_url).

        Raises DownloadError/SecurityError; caller handles atomic rename.
        """
        validated = validate_url(url, self.security, purpose=purpose)
        last_error: Exception | None = None
        for attempt in range(max(self.cfg.retries, 0) + 1):
            try:
                cur, hop = validated, 0
                while True:
                    req = self._client.build_request("GET", cur, headers=self._headers(headers))
                    resp = self._client.send(req, stream=True)
                    if resp.is_redirect:
                        cur = validate_redirect(cur, resp.headers.get("location", ""), self.security, hop)
                        hop += 1
                        resp.close()
                        continue
                    if resp.status_code in RETRYABLE_STATUS and attempt < self.cfg.retries:
                        retry_after = resp.headers.get("retry-after")
                        resp.close()
                        last_error = DownloadError(f"download returned HTTP {resp.status_code}")
                        self._sleep_backoff(attempt, retry_after)
                        break
                    if resp.status_code >= 400:
                        code = resp.status_code
                        resp.close()
                        raise DownloadError(f"download returned HTTP {code} for {url}")

                    import hashlib

                    h = hashlib.sha256()
                    total = 0
                    content_type = resp.headers.get("content-type", "").split(";")[0].strip() or None
                    try:
                        with open(dest_part, "wb") as f:
                            for chunk in resp.iter_bytes(65536):
                                total += len(chunk)
                                if total > max_bytes:
                                    raise SecurityError(
                                        f"download of {url} exceeded max_bytes={max_bytes}"
                                    )
                                h.update(chunk)
                                f.write(chunk)
                                if on_chunk:
                                    on_chunk(chunk)
                        return h.hexdigest(), total, content_type, str(resp.url)
                    finally:
                        resp.close()
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = DownloadError(f"download transport failure: {exc}")
                if attempt < self.cfg.retries:
                    self._sleep_backoff(attempt, None)
                    continue
            except (SecurityError, DownloadError):
                raise
        raise last_error or DownloadError(f"download failed for {url}")


def urljoin_safe(base: str, location: str) -> str:
    return urljoin(base, location)
