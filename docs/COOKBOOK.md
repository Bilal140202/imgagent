# imgagent Cookbook — task recipes for agents and pipelines

Each recipe is a complete, copy-runnable pattern. All of them assume the
core contract from [AGENTS.md](AGENTS.md): `--json` on stdout, one JSON
object, manifest = deliverable, exit codes 0/1/2/3.

---

## 1. Fill N verified images for one scene (the standard loop)

Goal: a scene needs N rights-cleared images; you want exactly N or an honest
shortfall.

```python
import json, subprocess, pathlib

def fill_scene(query: str, n: int, project: str, scene: str,
               workspace: str = ".imgagent") -> list[dict]:
    r = subprocess.run(
        ["imgagent", "run", query,
         "--require-reuse-rights", "--limit", "40", "--top", str(n),
         "--project", project, "--scene", scene,
         "--workspace", workspace, "--json"],
        capture_output=True, text=True)
    payload = json.loads(r.stdout)          # exit 2/3 raise here — fix flags
    data = payload["data"]
    manifest = json.loads(pathlib.Path(data["manifest_path"]).read_text())
    assets = manifest["assets"]
    if len(assets) < n or data.get("partial"):
        print(f"shortfall: {len(assets)}/{n}; errors={data.get('errors')}")
    return assets
```

Why it works: `run` is idempotent, so you can call `fill_scene` again with a
rephrased query and a *different* project/scene label; verified files are
never re-downloaded.

## 2. Curate-first pipeline (search → your judgment → download)

When an agent should look before it leaps: search widely, read the
candidates, then download an explicit selection.

```bash
imgagent search "Fjellstue Norway mountain cabin" --limit 40 --json > cand.json
imgagent list --job latest --rights verified_public_domain --min-width 1600 --json
# ...your selection logic...
imgagent download --job latest --ids cand-1a2b,cand-3c4d --project hytte --scene s1 --json
```

Selection heuristics that work well: prefer candidates with non-null
`license_name` and `width ≥ 2000`; treat `relevance_score` as a tiebreaker,
never as a rights signal.

## 3. Batch: many queries → one project, resumable

```bash
for q in "Isdal Woman 1970" "Bergen 1970s street" "Isdalsbygda landscape"; do
  imgagent run "$q" --require-reuse-rights --top 5 \
    --project isdal-doc --scene "$(echo "$q" | tr ' ' '-')" \
    --out ./assets --json >> batch.jsonl 2>> batch.err || echo "exit $? for $q" >> batch.err
done
```

Each line of `batch.jsonl` is an independent result payload; `batch.err`
collects failures per query. Re-run the whole loop freely — duplicates come
back as `duplicate`/`skipped_exists`.

## 4. Inspect-before-download on foreign URLs

You found a link elsewhere (a page, a pin, a news photo) and want to know
what it *actually* is before committing:

```bash
imgagent inspect "https://commons.wikimedia.org/wiki/File:Isdalen.jpg" --json
imgagent inspect "https://example.com/article" --json
```

`inspect` resolves page → image, sniffs magic bytes, reports dimensions,
format and byte size without writing assets. Use it to populate your own
candidate lists from sources imgagent doesn't integrate.

## 5. Manifest → render pipeline (renderer side)

```python
import hashlib, json, pathlib

manifest = json.loads(pathlib.Path(sys.argv[1]).read_text())
policy = {"verified_public_domain", "verified_license"}   # commercial gate

by_scene: dict[str, list] = {}
for a in manifest["assets"]:
    if a["rights_status"] not in policy:
        continue
    p = pathlib.Path(a["file"]["path"])
    assert p.exists() and hashlib.sha256(p.read_bytes()).hexdigest() == a["file"]["sha256"]
    by_scene.setdefault(a["scene"] or "_", []).append(a)

for scene, assets in sorted(by_scene.items()):
    print(scene, "→", [a["asset_id"] for a in assets])
```

Keep the manifest next to your project files: it is the provenance record
you show when someone asks "where did these images come from?"

## 6. Watchdog: provider health before a big batch

```bash
imgagent status --json | python3 -c '
import json,sys
s=json.load(sys.stdin)["data"]
down=[n for n,p in s["sources"].items() if not p.get("healthy", False)]
print("skip sources:", ",".join(down) or "none")'
# then append: --sources wikimedia,openverse   (minus the down ones)
```

## 7. Bring your own provider (official API you have a key for)

`config.toml` in the workspace:

```toml
[sources.custom.my_archive]
type = "json_api"
search_url = "https://api.example.org/v1/search?q={query}&limit={limit}"
api_key_env = "MY_ARCHIVE_KEY"          # read from env, never logged
json_path = "results"                    # where the array lives
field_map = { image_url = "url", title = "caption", width = "w", height = "h" }
license_field = "license"
source_page_url_field = "page_url"
```

```bash
MY_ARCHIVE_KEY=… imgagent search "query" --sources my_archive --json
```

Every provider result passes the same normalization, ranking, verification
and manifest pipeline as built-in sources.

## 8. CI usage (deterministic, no browser)

```yaml
- run: pip install imgagent
- run: |
    imgagent run "${{ matrix.query }}" --require-reuse-rights --top 3 \
      --project docs --scene "${{ matrix.slug }}" --json > manifest.json
- run: python scripts/verify_manifest.py manifest.json   # sha256 re-check
```

Set `IMGAGENT_WORKSPACE: ${{ runner.temp }}/.imgagent` when the checkout is
what must stay clean.
