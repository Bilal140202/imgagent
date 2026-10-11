"""Optional MCP server exposing the same business logic as CLI (no duplicates).

Requires the `mcp` extra:  pip install 'imgagent[mcp]'
Run with:                   imgagent-mcp   (stdio transport)

Tools wrap imgagent.api functions 1:1 and return JSON-serializable dicts.
"""

from __future__ import annotations

from imgagent.errors import DependencyError


def build_server():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover
        raise DependencyError(
            "MCP extra not installed. Install with: pip install 'imgagent[mcp]'"
        ) from exc

    from imgagent import api

    mcp = FastMCP("imgagent")

    @mcp.tool()
    def search_images(query: str, limit: int = 20, sources: list[str] | None = None,
                      require_reuse_rights: bool = False) -> dict:
        """Search multiple image sources and store normalized, ranked candidates (no downloads)."""
        return api.search_images(query, limit=limit, sources=sources,
                                 require_reuse_rights=require_reuse_rights or None)

    @mcp.tool()
    def list_candidates(job_id: str = "latest", limit: int = 50, offset: int = 0) -> dict:
        """List candidates for a job (human table equivalent, JSON)."""
        return api.list_candidates(job_id, limit=limit, offset=offset)

    @mcp.tool()
    def inspect_image_url(url: str) -> dict:
        """Inspect a URL (page or image) without downloading its contents."""
        return api.inspect_image_url(url)

    @mcp.tool()
    def download_selected(candidate_ids: list[str], destination: str | None = None,
                          project: str = "default", scene: str | None = None) -> dict:
        """Download selected candidates, validate, record assets."""
        return api.download_selected(candidate_ids, destination, project=project, scene=scene)

    @mcp.tool()
    def run_image_job(query: str, limit: int = 20, top: int = 5, project: str = "default") -> dict:
        """One-command pipeline: search -> rank -> download top N -> manifest."""
        return api.run_image_job(query, limit=limit, top=top, project=project)

    @mcp.tool()
    def get_manifest(job_id: str = "latest") -> dict:
        """Return the manifest (the stable contract with downstream consumers)."""
        return api.get_manifest(job_id)

    @mcp.tool()
    def get_job(job_id: str = "latest") -> dict:
        """Job record with per-source provenance events."""
        return api.get_job(job_id)

    return mcp


def main() -> int:
    try:
        server = build_server()
    except DependencyError as exc:
        import sys
        print(f"imgagent-mcp: {exc}", file=sys.stderr)
        return 3
    server.run()  # stdio
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
