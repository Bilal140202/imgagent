"""imgagent agent-instructions — the complete machine-facing contract."""

from __future__ import annotations

from imgagent import __version__
from imgagent.constants import INSTRUCTIONS_SCHEMA

INSTRUCTIONS = f"""# imgagent agent instructions (schema: {INSTRUCTIONS_SCHEMA}, version {__version__})

imgagent discovers, ranks, downloads and verifies images from public sources.
You (a coding/AI agent) can use it WITHOUT asking a human anything. Follow
this contract exactly.

## GOLDEN RULES
1. Call `imgagent ... --json` and parse EXACTLY ONE JSON object from stdout.
   Diagnostics always go to stderr — never parse stderr as JSON, never mix.
2. Exit codes: 0 = success (payload may contain `"partial": true` with a
   non-empty `errors` list), 1 = no usable results / operation failed,
   2 = invalid input or usage, 3 = configuration or dependency problem.
3. NEVER trust a file until it is in a manifest: verified assets carry
   `sha256`, dimensions and `rights_status`. Manifest = contract.
4. Missing data is `null` — do not guess. Rights states: verified_public_domain,
   verified_license, permission_required, unknown, restricted.
5. Discovery and download are separate concerns. `search` never downloads.
   `run` downloads only the top-N selection.

## ONE-COMMAND WORKFLOW
    imgagent run "QUERY TEXT" --limit 20 --top 5 --out ./assets --json

stdout payload (schema imgagent.result/v1):
    {{ "ok": true, "schema": "imgagent.result/v1", "command": "run",
       "data": {{ "job_id": "j-...", "candidates": [...], "downloads": {{...}},
                  "manifest_path": "/abs/path/.imgagent/manifests/j-....json",
                  "partial": false, "errors": [] }} }}

## STAGE-BY-STAGE
    imgagent search "QUERY" --limit 30 --sources wikimedia,openverse --json
    imgagent list --job latest [--source wikimedia] [--downloaded] [--rights verified_public_domain] --json
    imgagent download --job latest --top 10 --out ./assets --project myfilm --scene scene-04 --json
    imgagent download --ids cand-abc,cand-def --out ./assets --json
    imgagent inspect "https://example.org/page-or-image" --json
    imgagent status --json
    imgagent agent-instructions        # this text

## CANDIDATE SCHEMA (imgagent.candidate/v1, abridged)
    candidate_id, provider, title, image_url, thumbnail_url, source_page_url,
    creator, license_name, license_url, attribution, width, height, format,
    byte_size, search_query, retrieved_at, relevance_score, quality_score,
    authenticity, rights_status, download_status

## MANIFEST (imgagent.manifest/v1)
    Written to <workspace>/manifests/<job_id>.json after every download:
    {{ "schema_version": "imgagent.manifest/v1", "job": {{...}},
       "assets": [ {{ "asset_id": "asset-...", "file": {{ "path", "sha256",
         "byte_size", "width", "height", "format", "thumbnail_path" }},
         "provenance": {{ "provider", "source_url", "source_page_url",
           "creator", "license_name", "license_url", "attribution" }},
         "rights_status", "authenticity", "project", "scene" }} ] }}
    Consumers (e.g. Opuscar Studio) read ONLY this file.

## RIGHTS POLICY
    Use `--require-reuse-rights` to keep only verified_public_domain /
    verified_license candidates. Unknown rights NEVER pass this gate.
    A relevance score cannot override the gate. For commercial publication,
    review license notices in `attribution` (CC BY* requires attribution).

## FAILURE MODES (all explicit)
    - A broken/unreachable source appears in `errors[]`, other sources continue.
    - HTML pages disguised as images, corrupt files, oversized payloads and
      empty responses are rejected by magic-byte + full decode validation.
    - Downloads are atomic (.part then rename); a candidate only becomes
      `downloaded` after validation and final path existence.
    - Re-running search is idempotent (stable candidate ids); downloads reuse
      existing verified files (status duplicate/skipped_exists).

## WORKSPACE
    Default workspace: ./.imgagent (config.toml, database.sqlite, manifests/,
    cache/). Assets: ./assets/<project>/originals|processed|thumbnails/
    metadata.jsonl. Override with --workspace / IMGAGENT_WORKSPACE.
    Ephemeral storage warning appears if the workspace is not writable.

## COMMON RECIPES
    # Rights-cleared historical photo set for a documentary scene
    imgagent run "Isdal Woman Norway 1970" --require-reuse-rights --top 8 \\
        --project isdal --scene scene-04 --json

    # Inspect a Pinterest pin (public reference only) without downloading
    imgagent inspect "https://www.pinterest.com/pin/123456/" --json

    # Add your own official API provider (config.toml [sources.custom.*])
    imgagent status --json
"""


def get_instructions() -> str:
    return INSTRUCTIONS
