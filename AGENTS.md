# AGENTS.md — imgagent operator manual for AI agents

> **You are an AI agent that needs images.** This file is your complete
> operating manual: install, drive, verify and recover imgagent — without
> asking a human anything. Everything here is also available at runtime via
> `imgagent agent-instructions`, so you can re-load this contract mid-session
> even if your context was compacted.

imgagent discovers, ranks, downloads and **verifies** images from public
sources (Wikimedia Commons, Openverse, DuckDuckGo image results, any generic
web page, plus your own API providers). It never hands you a file it did not
fully decode, hash and record in a manifest. It runs fully deterministic —
no LLM, no browser, no paid API for the default workflow.

---

## 0. Install (30 seconds)

```bash
pip install imgagent                # PyPI: https://pypi.org/project/imgagent/
imgagent --version                  # sanity check
imgagent status --json              # provider health + workspace state
```

Optional extras: `pip install "imgagent[mcp]"` (MCP server), `pip install
"imgagent[avif]"` (AVIF decode support). Python ≥ 3.10, only `httpx` +
`Pillow` at runtime. Works on Linux / macOS / Windows / Termux / containers /
CI. No GPU, no browser, no network account.

---

## 1. The five golden rules

1. **Always pass `--json`.** stdout then carries *exactly one* JSON object;
   every diagnostic goes to stderr. Never parse stderr as JSON. Never mix
   logs into your parser.
2. **Respect exit codes.** `0` success (payload may still contain
   `"partial": true` + a non-empty `errors` list — inspect them), `1` no
   usable results / operation failed, `2` invalid input or usage (fix your
   arguments), `3` configuration or dependency problem (fix environment),
   `130` interrupted.
3. **Trust nothing before the manifest.** A file only counts as delivered
   when it appears in `<workspace>/manifests/<job_id>.json` with `sha256`,
   decoded `width`/`height`, and a `rights_status`. Paths outside a manifest
   are not deliverables.
4. **`null` means unknown — never guess.** Missing license, creator or
   dimensions stay `null`. Rights states are exactly:
   `verified_public_domain`, `verified_license`, `permission_required`,
   `unknown`, `restricted`. A search-engine hit is *never* evidence of
   rights.
5. **Search and download are separate stages.** `search` never downloads.
   `run` = search + rank + download top-N. When you need curation between
   the two, call the stages yourself.

---

## 2. Command map

| Goal | Command |
|---|---|
| Everything in one shot | `imgagent run "QUERY" --limit 20 --top 5 --out ./assets --json` |
| Discovery only | `imgagent search "QUERY" --limit 30 --sources wikimedia,openverse --json` |
| Curate a previous search | `imgagent list --job latest [--source wikimedia] [--downloaded] [--rights verified_public_domain] --json` |
| Download selected | `imgagent download --job latest --top 10 --project myfilm --scene scene-04 --out ./assets --json` |
| Download exact candidates | `imgagent download --ids cand-abc,cand-def --out ./assets --json` |
| Probe any URL before acting | `imgagent inspect "https://…" --json` |
| Health + workspace state | `imgagent status --json` |
| Re-read this contract | `imgagent agent-instructions` |
| Expose tools to an MCP client | `imgagent mcp` (or the `imgagent-mcp` entry point) |

Useful flags: `--require-reuse-rights` (drop candidates whose rights are not
verified), `--workspace PATH` (isolate jobs), `--project/--scene` (label
assets for the downstream renderer), `--min-width/--min-height`, `--format
jpeg,png`, `--max-bytes`.

---

## 3. The 60-second happy path

```bash
imgagent run "Isdal Woman Norway 1970 historical photographs" \
  --require-reuse-rights --top 8 --project isdal --scene scene-04 \
  --out ./assets --json > run.json 2> run.stderr
```

Then read `run.json`:

```json
{ "ok": true, "schema": "imgagent.result/v1", "command": "run",
  "data": { "job_id": "j-3f9c…", "candidates": [ … ],
            "downloads": { "saved": 8, "failed": 0, "duplicate": 1 },
            "manifest_path": "/abs/.imgagent/manifests/j-3f9c….json",
            "partial": false, "errors": [] } }
```

Hand `manifest_path` to your consumer. For each asset the manifest carries:
`asset_id`, verified `file.path` + `sha256` + dimensions, `provenance`
(provider, source URLs, creator, license), `rights_status`, `project`/
`scene`. A renderer needs nothing else — see
[docs/INTEGRATION.md](docs/INTEGRATION.md) for the consumption example.

**Verify before use (cheap, recommended):**

```python
import hashlib, json, pathlib
m = json.loads(pathlib.Path(manifest_path).read_text())
for a in m["assets"]:
    p = pathlib.Path(a["file"]["path"])
    assert hashlib.sha256(p.read_bytes()).hexdigest() == a["file"]["sha256"]
```

---

## 4. Programmatic API (when subprocess is wrong for you)

```python
from imgagent import (
    search_images, list_candidates, inspect_image_url,
    download_image, download_selected, run_image_job,
    get_job, get_manifest, get_asset_metadata,
)
result = run_image_job("Isdal Woman Norway 1970", top=8,
                       require_reuse_rights=True, project="isdal")
```

Same functions power the CLI, the MCP server and your Python code — one
implementation, one contract. Full signatures:
[docs/API_REFERENCE.md](docs/API_REFERENCE.md).

---

## 5. MCP setup (drop into your MCP client config)

```json
{ "mcpServers": { "imgagent": {
    "command": "imgagent-mcp",
    "args": [],
    "env": { "IMGAGENT_WORKSPACE": "/abs/path/.imgagent" } } } }
```

Tools mirror the API (`search_images`, `download_selected`, `get_manifest`, …)
with the same schemas — tool results are the same JSON objects the CLI
prints. Install once via `pip install "imgagent[mcp]"`.

---

## 6. Failure taxonomy — what to do

| Symptom | Meaning | Your move |
|---|---|---|
| `"partial": true`, `errors[]` non-empty, exit 0 | Some sources failed, others delivered | Use what's in the manifest; retry failed sources later or drop them via `--sources` |
| Exit 1, `ok: false` | Nothing usable for the query | Rephrase query (proper nouns + year + place help), widen `--limit`, add `--sources openverse,duckduckgo` |
| Exit 2 | Bad arguments / unknown source name | Read `error.message`, fix flags; source names validate upfront |
| Exit 3 | Config/dependency problem | Check `IMGAGENT_WORKSPACE` writability, config.toml syntax, Pillow present |
| Download stuck / slow | Source throttling | Cancel (Ctrl-C → exit 130); rerun is idempotent — verified files are reused, only missing ones download |
| `rights_status: "unknown"` everywhere | Sources gave no license metadata | Keep `--require-reuse-rights` on; don't downgrade the gate; use Wikimedia/Openverse which carry licenses |
| SSRF/private-IP rejection in `errors[]` | Security guard fired (correct) | Do not bypass; the target is non-public — that's by design |

**Idempotency guarantee:** re-running any stage is safe. Candidate IDs are
stable per (source, URL); already-verified downloads are reported as
`duplicate`/`skipped_exists`, not re-fetched.

---

## 7. Workspace & state

```
.imgagent/            workspace (override: --workspace / IMGAGENT_WORKSPACE)
  config.toml         optional config (custom providers, policies)
  database.sqlite     job + candidate state (WAL, transactional)
  manifests/          one JSON per download job  ← the contract
  cache/              HTTP metadata cache (TTL-bounded)
assets/<project>/
  originals/ processed/ thumbnails/  metadata.jsonl
```

Jobs survive restarts; `--job latest` addresses the most recent job. On
read-only filesystems imgagent warns and points `IMGAGENT_WORKSPACE` at a
writable path — never ignore that warning.

---

## 8. Rights policy — non-negotiable

- `--require-reuse-rights` admits **only** `verified_public_domain` and
  `verified_license`. `unknown` never passes. Relevance score cannot
  override the gate.
- For commercial publication: read `provenance.attribution`; CC BY* requires
  visible credit. Keep the manifest in your project archive — it *is* your
  provenance record.
- imgagent will not bypass logins, paywalls, CAPTCHAs or rate limits. If a
  source needs a key, add it via config `[sources.custom.*]`
  (see [docs/PROVIDERS.md](docs/PROVIDERS.md)) — keys come from env vars,
  never logs.

---

## 9. Safety envelope (already enforced, don't fight it)

HTTP/HTTPS only · SSRF guards (loopback/private/link-local/cloud-metadata
blocked, DNS-rebinding re-checked per hop) · per-redirect revalidation ·
path-traversal-proof writes · magic-byte + full image decode before
"downloaded" · hard byte caps · TLS verification never disabled · atomic
`.part` writes · no downloaded file is ever executed. Details:
[docs/SECURITY.md](docs/SECURITY.md).

---

## 10. Agent anti-patterns (do NOT)

- ❌ Scrape stdout without `--json` and grep for paths — parse the payload.
- ❌ Treat `candidates[]` as deliverables — only manifest assets are real.
- ❌ Retry `run` in a loop on exit 2 — that's your bug, not the tool's.
- ❌ Assume image rights from "it appeared in results".
- ❌ Disable TLS verification or proxy around SSRF guards. The guards are
  the product.
- ❌ Write into `.imgagent/` yourself — it's imgagent's state; use the API.

## 11. Where everything lives

| Artifact | Path |
|---|---|
| Agent contract (runtime) | `imgagent agent-instructions` |
| CLI reference | [docs/CLI_REFERENCE.md](docs/CLI_REFERENCE.md) |
| API reference | [docs/API_REFERENCE.md](docs/API_REFERENCE.md) |
| Provider matrix + limits | [docs/PROVIDERS.md](docs/PROVIDERS.md) |
| Integration recipes (renderer side) | [docs/INTEGRATION.md](docs/INTEGRATION.md) |
| Security model | [docs/SECURITY.md](docs/SECURITY.md) |
| Task cookbook | [docs/COOKBOOK.md](docs/COOKBOOK.md) |
| Landing page | https://superz1402.github.io/imgagent-site/ |
| PyPI | https://pypi.org/project/imgagent/ |
| Source | https://github.com/Bilal140202/imgagent |
