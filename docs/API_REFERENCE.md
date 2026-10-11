# imgagent programmatic API reference

`from imgagent import api` — synchronous functions with stable contracts.
They return JSON-safe dicts and raise the imgagent error taxonomy
(`imgagent.errors`): `UsageError` (exit 2), `ConfigError`/`DependencyError`
(exit 3), `NotFoundError`, `ProviderError`, `SecurityError`, `DownloadError`,
`ValidationFailed`. All functions accept `workspace=` to address a specific
workspace; omit it to use the default resolution (env/cwd).

## search_images

```python
api.search_images(query, *, limit=20, sources=None, min_width=None,
                  min_height=None, max_bytes=None, require_reuse_rights=None,
                  prefer_authentic=None, workspace=None)
-> {"job_id": str, "candidates": [candidate_dict...], "errors": [...]}
```

Creates a job, queries every source (failures isolated per source), stores
ranked candidates. Never downloads.

## list_candidates

```python
api.list_candidates(job_id="latest", *, provider=None, downloaded=None,
                    rights_status=None, min_width=None, limit=50, offset=0,
                    workspace=None)
-> {"job": {...}, "total": int, "offset": int, "limit": int, "candidates": [...]}
```

## inspect_image_url

```python
api.inspect_image_url(url, *, workspace=None)
-> {"kind": "image"|"page"|"error", "url", "candidate"?, "page_candidates"?, "notes"}
```

Wikimedia file pages resolve through the official API; everything else goes
through the generic page extractor. No content is persisted.

## download_selected / download_image

```python
api.download_selected(candidate_ids, destination=None, *, project=None,
                      scene=None, workspace=None, progress=None)
-> {"results": [download_result...], "summary": {"attempted", "downloaded",
                                                 "duplicate", "skipped_exists",
                                                 "failed", "errors"}}
api.download_image(candidate_id, ...)  # single-candidate convenience
```

`progress` is an optional callback receiving each result dict as it completes;
it must not write to stdout (JSON purity belongs to the caller).

## run_image_job

```python
api.run_image_job(query, *, limit=20, top=5, sources=None, min_width=None,
                  min_height=None, max_bytes=None, require_reuse_rights=None,
                  prefer_authentic=None, project=None, scene=None,
                  destination=None, workspace=None, progress=None)
-> {"job_id", "candidates", "downloads", "download_results", "errors",
    "manifest_path", "partial"}
```

## get_job / get_manifest / get_asset_metadata

```python
api.get_job(job_id="latest")   -> job dict incl. per-source provenance events
api.get_manifest(job_id="latest") -> manifest dict (imgagent.manifest/v1)
api.get_asset_metadata(asset_id)  -> asset dict
```

## MCP

`imgagent-mcp` exposes `search_images`, `list_candidates`, `inspect_image_url`,
`download_selected`, `run_image_job`, `get_manifest`, `get_job` as MCP tools
wrapping exactly these functions — no parallel business logic.

## Error handling example

```python
from imgagent import api
from imgagent.errors import ImgagentError

try:
    payload = api.run_image_job("Isdal Woman 1970", top=3,
                                require_reuse_rights=True)
    for asset in api.get_manifest(payload["job_id"])["assets"]:
        print(asset["file"]["path"], asset["rights_status"])
except ImgagentError as exc:
    print(f"imgagent failed: {exc.kind}: {exc}")
```
