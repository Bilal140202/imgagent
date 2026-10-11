# imgagent CLI reference (v0.1.0)

Global flags (work before or after the subcommand):

| Flag | Effect |
|------|--------|
| `--workspace PATH` | workspace directory (default `./.imgagent`) |
| `--json` | emit exactly one JSON object on stdout |
| `--quiet` | suppress human log lines on stderr |
| `--version` | print version and exit |

Diagnostics **always** go to stderr. Exit codes: `0` success (possibly explicit
partial), `1` no usable results / failed operation, `2` invalid usage,
`3` configuration or dependency problem, `130` interrupted.

## imgagent run QUERY

One-command pipeline: search → rank → dedup → download top-N → validate → manifest.

```
imgagent run "Isdal Woman Norway 1970 historical photographs" \
  --limit 20 --top 5 --out ./assets --project isdal --scene scene-04 --json
```

Options: `--limit N` (candidates kept, default 20) · `--top N` (downloaded,
default 5) · `--sources a,b,c` · `--out DIR` (assets root) · `--project NAME` ·
`--scene NAME` · `--min-width/--min-height N` · `--max-bytes N` ·
`--require-reuse-rights` · `--prefer-authentic` · `--no-download` ·
`--no-diversity` (strict ranked order, no host-variety pass).

## imgagent search QUERY

Discovery + ranking only; nothing is downloaded. Creates a resumable job.

```
imgagent search "Bergen 1970" --limit 30 --sources wikimedia,openverse --json
```

## imgagent list

```
imgagent list --job latest --json
imgagent list --job j-20261011T220229-5641 --source wikimedia --limit 100 --offset 0
imgagent list --downloaded --rights verified_public_domain --min-width 1200
imgagent list --out ./assets/isdal          # scan a directory instead of the DB
```

## imgagent download

```
imgagent download --job latest --top 10 --out ./assets --project film --json
imgagent download --ids cand-abc,cand-def --scene scene-02 --json
```

Selection: `--ids` (stable candidate ids) or `--top N` ranked. Re-downloads are
no-ops (`skipped_exists`/`duplicate` statuses) — safe to retry after crashes.

## imgagent inspect URL

Fetches metadata only (bounded probe ≤ `--max-probe-bytes`, default 1 MiB).

- Wikimedia file pages → resolved via the official API (full license metadata).
- Any webpage → og:image, JSON-LD, `<img>` extraction (public pages only).
- Direct image URLs → magic-byte/format probe.
- Pinterest pin URLs → resolved as public page references; the pin URL is
  preserved as `source_page_url`. No login/ToS bypass is attempted.

## imgagent status

Workspace paths, counts, storage usage. `--check-orphans` lists files not
referenced by the database; `--check-sources` health-checks every enabled
provider (network).

## imgagent agent-instructions

Prints the complete machine-facing contract (schema
`imgagent.agent-instructions/v1`): golden rules, JSON envelope, exit codes,
manifest schema, failure modes, recipes. An agent reading only this output can
operate imgagent without human help.

## imgagent mcp

Runs the MCP server over stdio (`imgagent-mcp` entry point). Requires
`pip install 'imgagent[mcp]'`. Tools: `search_images`, `list_candidates`,
`inspect_image_url`, `download_selected`, `run_image_job`, `get_manifest`,
`get_job` — the same functions the CLI uses.
