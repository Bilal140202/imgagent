# imgagent

**Universal image discovery, search, listing, download and asset agent — standalone, headless, deterministic, machine-friendly.**

[![CI](https://github.com/superz1402/imgagent/actions/workflows/ci.yml/badge.svg)](https://github.com/superz1402/imgagent/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

`imgagent` is a production-grade, installable image acquisition agent for AI agents and humans:
one command takes a query from **search → normalize → rank → dedupe → download → validate →
manifest → verified local paths**. Every stage is also independently callable. It runs fully
deterministic without any LLM, works headless on Linux / macOS / Windows / Termux / containers /
CI, and needs no paid API for its default public-source workflow.

Sibling doctrine of [ytagent](https://github.com/Bilal140202/ytagent),
[ttagent](https://github.com/Bilal140202/ttagent) and
[igagent](https://github.com/Bilal140202/igagent): one input, one JSON contract, verified bytes,
honest negatives.

---

## Quickstart

```bash
# From a clone
pip install .                # or pipx install . once published to PyPI

# One command: discover, rank, download the top 5, write the manifest
imgagent run "Isdal Woman Norway 1970 historical photographs" \
  --limit 20 --top 5 --out ./assets --json

# Search only (no downloads)
imgagent search "Isdal Woman Bergen" --limit 30 --json

# List candidates of the latest job
imgagent list --job latest --json

# Download selected results
imgagent download --job latest --top 10 --out ./assets --project myfilm --scene scene-04 --json

# Inspect a URL without downloading its contents
imgagent inspect "https://commons.wikimedia.org/wiki/File:Isdal_Woman.jpg"

# The complete machine-facing contract
imgagent agent-instructions
```

> `pipx install imgagent` will work once the package is published on PyPI — until then install
> from a clone. Do not trust any install instructions that point at a non-existent release.

## One-command workflow

```
INPUT QUERY
→ SEARCH MULTIPLE SOURCES      (wikimedia · openverse · duckduckgo · custom official APIs)
→ NORMALIZE RESULTS            (single ImageCandidate schema, versioned)
→ RANK RELEVANCE               (text + entity + technical + rights, deterministic, no LLM)
→ DEDUPLICATE                  (cross-provider URL dedup; SHA-256 + perceptual near-dup)
→ LIST CANDIDATES
→ DOWNLOAD SELECTED IMAGES     (streaming, bounded concurrency, atomic .part)
→ VALIDATE FILES               (magic bytes + full decode; never trust headers)
→ SAVE METADATA AND MANIFEST   (.imgagent/manifests/<job>.json + metadata.jsonl)
→ RETURN VERIFIED PATHS
```

A broken provider never breaks the others: per-source failures come back as structured entries
in `errors[]`, and a partial success is explicitly represented in the JSON payload.

## Supported providers

| Provider    | Type                              | API key | Default | Status | Notes |
|-------------|-----------------------------------|---------|---------|--------|-------|
| `wikimedia` | Official MediaWiki API            | no      | yes     | tested | License metadata, originals, best historical coverage |
| `openverse` | Official Openverse API            | optional (`OPENVERSE_API_KEY`) | yes | tested | CC/public-domain focus, rights-cleared mode |
| `duckduckgo`| Community vqd flow (unofficial)   | no      | yes     | best-effort | Volatile; failures are graceful and explicit |
| `generic`   | Webpage extractor (og:image, JSON-LD, `<img>`) | no | inspect-only | tested | Also resolves public Pinterest pin pages as page references |
| `custom:*`  | Config-driven official APIs (Unsplash, Pexels, your own) | per-config | opt-in | tested | Declared in `config.toml [sources.custom.*]` |

Google Images is deliberately **not** implemented as fragile HTML scraping; use a configured
official API (custom source) instead. No CAPTCHA, paywall, login or access-control bypass exists
in this codebase. See [docs/PROVIDERS.md](docs/PROVIDERS.md) for the full support matrix.

## Verification & security

- Downloads are validated by **magic bytes + full Pillow decode** — HTML pages masquerading as
  images, corrupt files, empty files and oversized payloads are rejected.
- **SHA-256** content hashing, exact-duplicate reuse, atomic `.part` → rename writes.
- **SSRF protection**: http/HTTPS only, private/link-local/loopback ranges blocked,
  cloud metadata endpoints hard-blocked, redirect-by-redirect validation, response size caps.
- TLS verification is never disabled as a fallback. See [docs/SECURITY.md](docs/SECURITY.md).

## Rights & provenance

Technical downloadability is separated from publication permission. Every asset records source
URL, provider, creator, license name/URL, attribution text, retrieval timestamps, file hash,
dimensions and a rights state:

`verified_public_domain` · `verified_license` · `permission_required` · `unknown` · `restricted`

`--require-reuse-rights` keeps only verified states — and a high relevance score can never
override that gate. Generic illustration and authentic case evidence remain distinguishable via
`authenticity` and `--prefer-authentic`.

## Workspace layout

```
.imgagent/
  config.toml          # optional configuration
  jobs/
  database.sqlite      # transactional job/candidate/asset state (WAL)
  manifests/           # <job_id>.json — the stable contract for consumers
  cache/               # TTL JSON cache for API responses

assets/
  <project>/
    originals/         # verified originals only
    processed/
    thumbnails/
    metadata.jsonl     # one record per downloaded asset
```

The workspace is configurable (`--workspace`, `IMGAGENT_WORKSPACE`); jobs resume after
interruption and repeated invocations never re-download verified files.

## Machine integration

- `--json` emits **exactly one** JSON object on stdout; diagnostics go to stderr, always.
- Exit codes: `0` success (incl. explicit partial), `1` no usable results, `2` usage, `3` config/dependency.
- `imgagent agent-instructions` prints the complete contract an agent can follow without a human.
- Optional MCP server (`pip install 'imgagent[mcp]'` + `imgagent-mcp`) wraps the same functions.
- Programmatic API: `from imgagent import api` — `search_images`, `list_candidates`,
  `download_selected`, `run_image_job`, `get_manifest`, … See [docs/API_REFERENCE.md](docs/API_REFERENCE.md).

## Configuration

Copy [`config.example.toml`](config.example.toml) into `.imgagent/config.toml` or set
environment variables (`IMGAGENT_*`). Everything works with defaults; API keys are read from
environment variables only and are never logged.

## Environments

Python ≥ 3.10, ~2 runtime dependencies (`httpx`, `Pillow`). CPU-only, no browser required for
the default provider set. `Dockerfile` included; GitHub Actions CI runs the full test suite on
3.10–3.13. For read-only filesystems point `IMGAGENT_WORKSPACE` at a writable path — imgagent
detects and warns before work is lost.

## Development

```bash
pip install -e '.[dev]'
pytest                    # unit suite (mock servers, no network)
pytest -m integration     # opt-in live-source tests
ruff check src tests
```

## Authors & owners

- **[superz1402](https://github.com/superz1402)** — author, owner
- **[Bilal140202](https://github.com/Bilal140202)** — author, owner
- **[ansaribilal14](https://github.com/ansaribilal14)** — author, owner
- **[hamza140202](https://github.com/hamza140202)** — author, owner

## License & attribution

MIT — see [LICENSE](LICENSE). imgagent was built as an independent implementation; it studies
the architecture of projects like gallery-dl (GPL) and img2dataset (Apache-2.0) **without
copying their code**. Downloaded images carry their own licenses — the manifest records them;
you are responsible for respecting them at publication time.
