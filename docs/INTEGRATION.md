# Integrating imgagent with agents and pipelines

imgagent is designed to be consumed three ways: as a CLI by autonomous agents,
as a Python library, and over MCP. The **manifest is the contract** — whatever
finds the images, the consumer reads one JSON file.

## 1. Manifest contract (for Opuscar Studio or any renderer)

After every download, `<workspace>/manifests/<job_id>.json` contains:

```json
{
  "schema_version": "imgagent.manifest/v1",
  "job": { "id": "j-...", "query": "...", "params": {...} },
  "assets": [
    {
      "asset_id": "asset-586f22ac8bf2e090fba6",
      "project": "isdal-demo",
      "scene": null,
      "file": { "path": "/abs/originals/isdalen--9ac9f850.jpg",
                "sha256": "586f...", "byte_size": 2062189,
                "width": 3264, "height": 2448, "format": "JPEG",
                "thumbnail_path": "/abs/thumbnails/isdalen--9ac9f850.jpg" },
      "provenance": { "provider": "wikimedia",
                      "source_url": "https://upload.wikimedia.org/...",
                      "source_page_url": "https://commons.wikimedia.org/wiki/File:...",
                      "creator": "...", "license_name": "Public domain",
                      "license_url": null, "attribution": null },
      "rights_status": "verified_public_domain",
      "authenticity": "unknown"
    }
  ]
}
```

A consumer needs to know nothing about how the image was found: stable
`asset_id`, verified local `path`, `sha256` for integrity, attribution and
`rights_status` for clearance, `project`/`scene` for assembly.

Example consumption:

```python
import json, subprocess, hashlib, pathlib

subprocess.run(["imgagent", "run", "Isdal Woman Norway 1970",
                "--require-reuse-rights", "--project", "isdal",
                "--json"], check=True, capture_output=True)

ws = pathlib.Path(".imgagent/manifests")
manifest = json.loads(sorted(ws.glob("*.json"))[-1].read_text())
for asset in manifest["assets"]:
    p = pathlib.Path(asset["file"]["path"])
    assert hashlib.sha256(p.read_bytes()).hexdigest() == asset["file"]["sha256"]
    if asset["rights_status"] not in ("verified_public_domain", "verified_license"):
        continue  # policy gate happens on your side too
    use_in_timeline(asset["project"], asset["scene"], p)
```

A commercially cleared publication pipeline should filter on
`rights_status` and keep `attribution` visible in credits. Generic
illustration and authentic case evidence stay distinguishable via
`authenticity` and the original `search_query`.

## 2. CLI for autonomous agents (ytagent doctrine)

Agents should read `imgagent agent-instructions` once and then operate
autonomously: `--json` gives one JSON object on stdout, stderr carries
diagnostics, exit codes are 0/1/2/3, and every failure mode is structured.
Re-runs are idempotent (stable candidate ids, hash-based asset reuse), so a
crashed pipeline can simply re-invoke the same command.

## 3. Python API

```python
from imgagent import api
payload = api.run_image_job("query", require_reuse_rights=True, project="doc")
manifest = api.get_manifest(payload["job_id"])
```

## 4. MCP

Add to an MCP client config:

```json
{ "mcpServers": { "imgagent": { "command": "imgagent-mcp" } } }
```

Tools map 1:1 to the Python API. Requires `pip install 'imgagent[mcp]'`.

## 5. Continuous asset farms (yt-farm pattern)

imgagent is workspace-based and resumable, so a scheduled worker (cron, GitHub
Actions on a self-hosted runner, or a container with a persistent volume) can
run discovery jobs repeatedly without re-downloading:

```bash
export IMGAGENT_WORKSPACE=/var/lib/imgagent/workspace
export IMGAGENT_ASSETS_ROOT=/var/lib/imgagent/assets
imgagent run "query batch item" --require-reuse-rights --top 10 --json
```

The database tracks every job and asset; export manifests independently of the
database at any time (they are plain JSON files).
