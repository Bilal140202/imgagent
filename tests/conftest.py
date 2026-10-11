"""Shared fixtures: mock server, workspaces, config, CLI runner helpers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from mock_server import MockServer  # noqa: E402


@pytest.fixture(scope="session")
def mock():
    server = MockServer().start()
    yield server
    server.stop()


@pytest.fixture()
def ws_dir(tmp_path) -> Path:
    """Isolated workspace with a config declaring the mock custom source."""
    return tmp_path / ".imgagent"


@pytest.fixture()
def write_config(ws_dir):
    def _write(base_url: str, extra: str = "") -> None:
        ws_dir.mkdir(parents=True, exist_ok=True)
        (ws_dir / "config.toml").write_text(f"""
[sources]
enabled = ["mock"]

[sources.custom.mock]
endpoint = "{base_url}/api/search"
method = "GET"
params = {{ q = "{{query}}", limit = "{{limit}}" }}
results_path = "items"
rights_status = "unknown"

[sources.custom.mock.fields]
image_url = "image"
thumbnail_url = "thumb"
title = "title"
source_page_url = "page"
width = "width"
height = "height"
creator = "author"
license_name = "license"
format = "fmt"

[sources.custom.broken]
endpoint = "{base_url}/notfound"
results_path = "items"

[sources.custom.broken.fields]
image_url = "image"
{extra}
""")
    return _write


@pytest.fixture()
def mock_env(monkeypatch, mock, write_config, ws_dir):
    """Point imgagent at the mock server with private addressing allowed (test mode)."""
    monkeypatch.setenv("IMGAGENT_WORKSPACE", str(ws_dir))
    monkeypatch.setenv("IMGAGENT_ASSETS_ROOT", str(ws_dir.parent / "assets"))
    monkeypatch.setenv("IMGAGENT_SOURCES", "mock")
    monkeypatch.setenv("IMGAGENT_ALLOW_PRIVATE", "1")
    monkeypatch.setenv("IMGAGENT_CACHE_TTL", "0")
    monkeypatch.setenv("IMGAGENT_READ_TIMEOUT", "5")
    write_config(mock.base_url)
    return mock


def run_cli(argv: list[str]) -> tuple[int, dict | None, str]:
    """Run the CLI in-process; returns (exit_code, stdout_json_or_None, stdout_raw)."""
    import io
    from contextlib import redirect_stdout

    from imgagent.cli import main

    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(argv)
    out = buf.getvalue()
    parsed = None
    if out.strip():
        try:
            parsed = json.loads(out)
        except json.JSONDecodeError:
            parsed = None
    return code, parsed, out
