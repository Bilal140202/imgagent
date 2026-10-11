"""Security policy tests: SSRF, metadata block, redirect validation, filenames."""

from __future__ import annotations

import pytest

from imgagent.errors import SecurityError
from imgagent.http import HttpClient, HttpConfig
from imgagent.security import SecurityOptions, sanitize_filename, validate_url

OPTS = SecurityOptions()                      # public mode
OPTS_PRIVATE = SecurityOptions(allow_private_addr=True)  # test/dev mode


def test_rejects_non_http_schemes():
    for url in ("ftp://example.com/a.jpg", "file:///etc/passwd", "data:image/png;base64,AAAA"):
        with pytest.raises(SecurityError):
            validate_url(url, OPTS)


def test_rejects_private_addresses():
    for host in ("127.0.0.1", "10.1.2.3", "192.168.1.5", "172.16.0.9", "169.254.1.1", "[::1]"):
        with pytest.raises(SecurityError):
            validate_url(f"http://{host}/img.png", OPTS)


def test_rejects_metadata_even_with_private_allowed():
    with pytest.raises(SecurityError):
        validate_url("http://169.254.169.254/latest/meta-data/", OPTS_PRIVATE)


def test_allows_private_when_flag_set():
    assert validate_url("http://127.0.0.1:8080/img.png", OPTS_PRIVATE).startswith("http://127.0.0.1")


def test_rejects_embedded_credentials():
    with pytest.raises(SecurityError):
        validate_url("http://user:pass@example.com/img.png", OPTS)


def test_rejects_nonstandard_ports_in_public_mode():
    with pytest.raises(SecurityError):
        validate_url("http://example.com:8080/img.png", OPTS)


def test_rejects_blocked_host_suffixes():
    with pytest.raises(SecurityError):
        validate_url("http://metadata.google.internal/computeMetadata/v1/", OPTS)


def test_unresolvable_host_rejected():
    with pytest.raises(SecurityError):
        validate_url("http://this-host-does-not-exist-9f3x.example/img.png", OPTS)


def test_sanitize_filename_blocks_traversal():
    assert ".." not in sanitize_filename("../../etc/passwd")
    assert "/" not in sanitize_filename("a/b/c")
    assert "\\" not in sanitize_filename("..\\..\\windows\\evil")
    assert sanitize_filename("") == "file"
    assert sanitize_filename(None) == "file"
    name = sanitize_filename("con")
    assert name.startswith("_")  # Windows reserved names


def test_redirect_to_metadata_is_blocked(mock):
    client = HttpClient(HttpConfig(security=OPTS_PRIVATE, retries=0, connect_timeout=3, read_timeout=3))
    try:
        with pytest.raises(SecurityError):
            client.download_stream(f"{mock.base_url}/redirect/meta", None, max_bytes=1000,
                                   purpose="download")
    finally:
        client.close()


def test_redirect_loop_hits_cap(mock):
    client = HttpClient(HttpConfig(security=OPTS_PRIVATE, retries=0, connect_timeout=3, read_timeout=3))
    try:
        with pytest.raises(SecurityError):
            client.get_json(f"{mock.base_url}/redirect/loop", purpose="probe", use_cache=False)
    finally:
        client.close()


def test_allowed_redirect_followed(mock, tmp_path):
    client = HttpClient(HttpConfig(security=OPTS_PRIVATE, retries=0, connect_timeout=3, read_timeout=3))
    try:
        sha, size, ctype, final = client.download_stream(
            f"{mock.base_url}/redirect/img", tmp_path / "part", max_bytes=1_000_000, purpose="download")
        assert size > 0 and ctype == "image/png" and final.endswith("/img/png/1")
    finally:
        client.close()


def test_user_agent_is_sent(mock):
    client = HttpClient(HttpConfig(security=OPTS_PRIVATE, retries=0, connect_timeout=3, read_timeout=3))
    try:
        data = client.get_json(f"{mock.base_url}/headers", purpose="probe", use_cache=False)
        assert "imgagent/" in data["ua"]
    finally:
        client.close()
