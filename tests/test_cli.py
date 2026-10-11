"""CLI contract tests: JSON envelope, exit codes, and the full e2e workflow
search -> list -> download -> verify -> manifest against the mock server."""

from __future__ import annotations

import json
import os
from pathlib import Path

from conftest import run_cli


def test_agent_instructions_contract(mock_env):
    code, _, raw = run_cli(["agent-instructions"])
    assert code == 0
    assert raw.startswith("# imgagent agent instructions")
    code2, payload, raw2 = run_cli(["agent-instructions", "--json"])
    assert code2 == 0
    assert payload["ok"] is True
    assert payload["schema"] == "imgagent.result/v1"
    assert "exit codes" in payload["data"]["instructions"].lower() or \
           "Exit codes" in payload["data"]["instructions"]
    assert json.loads(raw2)["data"]["instructions"].startswith("# imgagent")


def test_status_json_envelope(mock_env):
    code, payload, _ = run_cli(["status", "--json"])
    assert code == 0 and payload["ok"] is True and payload["command"] == "status"
    assert payload["data"]["counts"] == {"jobs": 0, "candidates": 0, "assets": 0}


def test_invalid_usage_exit_2(mock_env):
    code, payload, raw = run_cli(["download", "--json"])  # no --ids / --top
    assert code == 2
    assert payload["error"]["kind"] == "usage_error"


def test_unknown_source_exit_2(mock_env):
    code, payload, _ = run_cli(["search", "x", "--sources", "not-a-source", "--json"])
    assert code == 2


def test_no_results_exit_1(mock_env):
    code, payload, _ = run_cli(["search", "empty", "--json"])
    assert code == 1
    assert payload["ok"] is True and payload["data"]["total"] == 0


def test_missing_job_exit_1(mock_env):
    code, payload, _ = run_cli(["download", "--job", "j-nope", "--top", "1", "--json"])
    assert code == 1 and payload["error"]["kind"] == "not_found"


def test_e2e_workflow(mock_env, ws_dir):
    """The spec-mandated complete flow: search -> list -> download -> verify -> manifest."""
    base = mock_env.base_url

    # 1. search (no downloads)
    code, payload, _ = run_cli(["search", "isdal woman", "--limit", "5", "--json"])
    assert code == 0
    job_id = payload["data"]["job_id"]
    assert payload["data"]["total"] == 5
    top = payload["data"]["candidates"][0]
    assert top["rights_status"] in ("unknown", "verified_license")
    assert all(c["download_status"] == "not_downloaded" for c in payload["data"]["candidates"])

    # 2. list --job latest
    code, payload, _ = run_cli(["list", "--job", "latest", "--json"])
    assert code == 0 and payload["data"]["job_id"] == job_id and payload["data"]["total"] == 5

    # 3. download top 2 by id
    ids = ",".join(c["candidate_id"] for c in payload["data"]["candidates"][:2])
    code, payload, _ = run_cli(["download", "--ids", ids, "--project", "isdal", "--json"])
    assert code == 0
    downloads = payload["data"]["downloads"]
    assert downloads["downloaded"] == 2
    manifest_path = payload["data"]["manifest_path"]
    assert os.path.exists(manifest_path)

    # 4. manifest is the contract
    manifest = json.loads(Path(manifest_path).read_text())
    assert manifest["schema_version"] == "imgagent.manifest/v1"
    assert len(manifest["assets"]) == 2
    asset = manifest["assets"][0]
    assert asset["file"]["sha256"] and os.path.exists(asset["file"]["path"])
    assert asset["project"] == "isdal"
    assert asset["provenance"]["source_url"].startswith(base)

    # 5. verify: assets on disk, hashed correctly, metadata.jsonl appended
    for a in manifest["assets"]:
        import hashlib
        h = hashlib.sha256(open(a["file"]["path"], "rb").read()).hexdigest()
        assert h == a["file"]["sha256"]
    meta = ws_dir.parent / "assets" / "isdal" / "metadata.jsonl"
    lines = [json.loads(line) for line in meta.read_text().strip().splitlines()]
    assert len(lines) == 2

    # 6. re-download same ids -> skipped_exists, no re-download (image hits unchanged)
    def img_hits():
        return sum(v for k, v in mock_env.counts.items() if k.startswith("/img/"))
    hits_before = img_hits()
    code, payload, _ = run_cli(["download", "--ids", ids, "--json"])
    assert code == 0
    assert payload["data"]["downloads"]["skipped_exists"] == 2
    assert img_hits() == hits_before

    # 7. list --downloaded shows 2
    code, payload, _ = run_cli(["list", "--downloaded", "--json"])
    assert code == 0 and payload["data"]["total"] == 2


def test_run_command_full_pipeline(mock_env, ws_dir):
    code, payload, _ = run_cli(
        ["run", "isdal woman", "--limit", "5", "--top", "3", "--out",
         str(ws_dir.parent / "assets-run"), "--project", "runproj", "--json"])
    assert code == 0
    data = payload["data"]
    assert data["manifest_path"] and os.path.exists(data["manifest_path"])
    assert data["downloads"]["downloaded"] == 3
    results = data["download_results"]
    assert all(r["status"] in ("downloaded", "duplicate") for r in results)
    # assets root override respected
    assert any(r["path"].startswith(str(ws_dir.parent / "assets-run")) for r in results)


def test_run_with_source_failure_is_partial(mock_env):
    # one healthy source + one broken source -> partial success with structured error
    code, payload, _ = run_cli(["search", "isdal", "--sources", "mock,broken", "--json"])
    assert code == 0
    assert any(e["source"] == "broken" for e in payload["errors"])
    assert payload["data"]["total"] == 5


def test_json_stdout_purity(mock_env):
    """stdout must contain exactly one JSON object; diagnostics live on stderr."""
    code, payload, raw = run_cli(["run", "isdal", "--limit", "3", "--top", "1", "--json"])
    obj = json.loads(raw)  # raises if stdout is polluted
    assert obj["schema"] == "imgagent.result/v1"


def test_inspect_page(mock_env):
    code, payload, _ = run_cli(["inspect", f"{mock_env.base_url}/page/og", "--json"])
    assert code == 0
    assert payload["data"]["kind"] == "page"
    assert len(payload["data"]["page_candidates"]) >= 3


def test_config_exit_3(mock_env, monkeypatch, ws_dir):
    (ws_dir / "config.toml").write_text("not [valid toml ====")
    code, payload, _ = run_cli(["status", "--json"])
    assert code == 3 and payload["error"]["kind"] == "config_error"
