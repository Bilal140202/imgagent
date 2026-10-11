"""imgagent CLI.

Output contract:
- stdout carries ONLY the final human table OR one JSON object (--json).
- All logs/diagnostics go to stderr, always.
- Exit codes: 0 ok/partial, 1 no results/failed, 2 usage, 3 config/dependency.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from imgagent import __version__
from imgagent import constants as C
from imgagent.config import load_config
from imgagent.db import Database
from imgagent.errors import ConfigError, DependencyError, ImgagentError, NotFoundError, ProviderError, UsageError
from imgagent.humanize import (
    print_candidates_table,
    print_downloads_table,
    print_downloads_table_results,
    print_status_table,
)
from imgagent.instructions import get_instructions
from imgagent.jobs import Orchestrator, detect_orphans
from imgagent.workspace import Workspace


# --- envelope helpers ------------------------------------------------------------
def emit_json(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    sys.stdout.flush()


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def ok_envelope(command: str, data: dict, errors: list | None = None) -> dict:
    return {"ok": True, "schema": C.RESULT_SCHEMA, "command": command,
            "data": data, "errors": errors or []}


def err_envelope(command: str, kind: str, message: str) -> dict:
    return {"ok": False, "schema": C.RESULT_SCHEMA, "command": command,
            "error": {"kind": kind, "message": message}}


# --- parser ---------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    # SUPPRESS defaults let the same flag appear before or after the subcommand:
    # absent flags leave the main parser's value untouched instead of clobbering it.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--workspace", default=argparse.SUPPRESS,
                        help="workspace directory (default ./.imgagent)")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="emit a single JSON object on stdout")
    common.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS,
                        help="suppress human-readable log lines on stderr")

    p = argparse.ArgumentParser(
        prog="imgagent",
        description="imgagent — universal image discovery, search, listing, download and asset agent.",
        parents=[common])
    p.add_argument("--version", action="version", version=f"imgagent {__version__}")
    sub = p.add_subparsers(dest="command")

    sp = sub.add_parser("run", parents=[common], help="search -> rank -> download top N -> manifest")
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--top", type=int, default=5)
    sp.add_argument("--sources", help="comma-separated source list")
    sp.add_argument("--out", help="assets root directory")
    sp.add_argument("--project")
    sp.add_argument("--scene")
    sp.add_argument("--min-width", type=int)
    sp.add_argument("--min-height", type=int)
    sp.add_argument("--max-bytes", type=int)
    sp.add_argument("--require-reuse-rights", action="store_true")
    sp.add_argument("--prefer-authentic", action="store_true")
    sp.add_argument("--no-download", action="store_true", help="discovery only")
    sp.add_argument("--no-diversity", action="store_true", help="strict ranked top-N, no host variety")

    sp = sub.add_parser("search", parents=[common], help="discover and rank candidates (no downloads)")
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--sources")
    sp.add_argument("--min-width", type=int)
    sp.add_argument("--min-height", type=int)
    sp.add_argument("--max-bytes", type=int)
    sp.add_argument("--require-reuse-rights", action="store_true")
    sp.add_argument("--prefer-authentic", action="store_true")

    sp = sub.add_parser("list", parents=[common], help="list candidates or downloaded assets")
    sp.add_argument("--job", default="latest")
    sp.add_argument("--source")
    sp.add_argument("--downloaded", action="store_true")
    sp.add_argument("--rights", dest="rights_status")
    sp.add_argument("--min-width", type=int)
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--offset", type=int, default=0)
    sp.add_argument("--out", help="scan an assets directory instead of the database")

    sp = sub.add_parser("download", parents=[common], help="download selected candidates")
    sp.add_argument("--job", default="latest")
    sp.add_argument("--ids", help="comma-separated candidate ids")
    sp.add_argument("--top", type=int)
    sp.add_argument("--out", help="assets root directory")
    sp.add_argument("--project")
    sp.add_argument("--scene")
    sp.add_argument("--max-bytes", type=int)

    sp = sub.add_parser("inspect", parents=[common], help="inspect a URL without downloading contents")
    sp.add_argument("url")
    sp.add_argument("--max-probe-bytes", type=int, default=1_048_576)

    sp = sub.add_parser("status", parents=[common], help="workspace, storage and source status")
    sp.add_argument("--check-orphans", action="store_true")
    sp.add_argument("--check-sources", action="store_true", help="probe every enabled source (network)")

    sub.add_parser("agent-instructions", parents=[common], help="print the machine-facing contract")
    sub.add_parser("mcp", parents=[common], help="run the MCP server (stdio; requires imgagent[mcp])")
    return p


# --- command implementations -------------------------------------------------------
def _split_sources(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    sources = [s.strip() for s in raw.split(",") if s.strip()]
    return sources or None


def cmd_run(args) -> tuple[dict, int]:
    cfg = load_config(Path(getattr(args, "workspace", None)).expanduser() if getattr(args, "workspace", None) else None,
                      cli_overrides={"sources": _split_sources(args.sources),
                                     "max_bytes": args.max_bytes,
                                     "require_reuse_rights": args.require_reuse_rights or None,
                                     "prefer_authentic": args.prefer_authentic or None})
    orch = Orchestrator(cfg)
    try:
        if args.no_download:
            job_id, ranked, errors = orch.search_only(
                args.query, limit=args.limit, sources=_split_sources(args.sources),
                min_width=args.min_width, min_height=args.min_height, max_bytes=args.max_bytes,
                require_reuse_rights=args.require_reuse_rights or None,
                prefer_authentic=args.prefer_authentic or None)
            data = {"job_id": job_id, "candidates": [c.to_dict() for c in ranked],
                    "downloads": None, "manifest_path": None, "partial": False}
            code = C.EXIT_OK if ranked else C.EXIT_NO_RESULTS
            return ok_envelope("run", data, errors), code

        payload = orch.run(
            args.query, limit=args.limit, top=args.top, sources=_split_sources(args.sources),
            min_width=args.min_width, min_height=args.min_height, max_bytes=args.max_bytes,
            require_reuse_rights=args.require_reuse_rights or None,
            prefer_authentic=args.prefer_authentic or None,
            project=args.project, scene=args.scene, out_root=args.out,
            diversity=not args.no_diversity)
        data = {"job_id": payload["job_id"],
                "candidates": payload["candidates"],
                "downloads": payload["downloads"],
                "download_results": payload.get("download_results"),
                "manifest_path": payload["manifest_path"],
                "partial": payload["partial"]}
        errors = payload["errors"]
        if payload["candidates"] and payload["downloads"] and payload["downloads"]["failed"] > 0 \
                and payload["downloads"]["downloaded"] == 0:
            return ok_envelope("run", data, errors), C.EXIT_NO_RESULTS
        code = C.EXIT_OK if payload["candidates"] else C.EXIT_NO_RESULTS
        return ok_envelope("run", data, errors), code
    finally:
        orch.close()


def cmd_search(args) -> tuple[dict, int]:
    cfg = load_config(Path(getattr(args, "workspace", None)).expanduser() if getattr(args, "workspace", None) else None,
                      cli_overrides={"sources": _split_sources(args.sources),
                                     "max_bytes": args.max_bytes,
                                     "require_reuse_rights": args.require_reuse_rights or None,
                                     "prefer_authentic": args.prefer_authentic or None})
    orch = Orchestrator(cfg)
    try:
        job_id, ranked, errors = orch.search_only(
            args.query, limit=args.limit, sources=_split_sources(args.sources),
            min_width=args.min_width, min_height=args.min_height, max_bytes=args.max_bytes,
            require_reuse_rights=args.require_reuse_rights or None,
            prefer_authentic=args.prefer_authentic or None)
        data = {"job_id": job_id, "total": len(ranked),
                "candidates": [c.to_dict() for c in ranked]}
        return ok_envelope("search", data, errors), (C.EXIT_OK if ranked else C.EXIT_NO_RESULTS)
    finally:
        orch.close()


def cmd_list(args) -> tuple[dict, int]:
    cfg = load_config(Path(getattr(args, "workspace", None)).expanduser() if getattr(args, "workspace", None) else None)
    if args.out:
        return _list_dir(args, cfg)
    db = Database((Workspace.from_config(cfg).ensure()).db_path)
    try:
        job = db.resolve_job_ref(args.job)
        cands, total = db.list_candidates(
            job.id, provider=args.source, downloaded=args.downloaded or None,
            rights_status=args.rights_status, min_width=args.min_width,
            limit=args.limit, offset=args.offset)
        data = {"job_id": job.id, "total": total, "offset": args.offset,
                "limit": args.limit, "candidates": [c.to_dict() for c in cands]}
        code = C.EXIT_OK if (cands or total) else C.EXIT_NO_RESULTS
        return ok_envelope("list", data), code
    finally:
        db.close()


def _list_dir(args, cfg) -> tuple[dict, int]:
    from imgagent.images import validate_image_file
    root = Path(args.out).expanduser()
    if not root.is_dir():
        raise UsageError(f"assets directory {args.out!r} does not exist")
    assets, skipped = [], 0
    for f in sorted(root.rglob("*")):
        if not f.is_file() or f.name.startswith(".") or f.suffix.lower() in (".jsonl", ".json"):
            continue
        try:
            info = validate_image_file(f, max_pixels=cfg.max_pixels)
            assets.append({"path": str(f.resolve()), "width": info.width, "height": info.height,
                           "format": info.format, "byte_size": f.stat().st_size})
        except Exception:
            skipped += 1
    data = {"assets_root": str(root), "total": len(assets), "unrecognized": skipped,
            "assets": assets[args.offset: args.offset + args.limit]}
    return ok_envelope("list", data), (C.EXIT_OK if assets else C.EXIT_NO_RESULTS)


def cmd_download(args) -> tuple[dict, int]:
    if not args.ids and args.top is None:
        raise UsageError("download requires --ids <id,id,...> or --top N (with --job)")
    cfg = load_config(Path(getattr(args, "workspace", None)).expanduser() if getattr(args, "workspace", None) else None,
                      cli_overrides={"max_bytes": args.max_bytes})
    orch = Orchestrator(cfg)
    try:
        ids = [i.strip() for i in args.ids.split(",") if i.strip()] if args.ids else None
        out = orch.download_from_job(
            args.job, top=args.top, ids=ids, project=args.project, scene=args.scene,
            out_root=args.out)
        data = {"job_id": out["job_id"], "manifest_path": out["manifest_path"],
                "project": out["project"], "partial": out["partial"],
                "downloads": out["summary"], "results": [r.to_dict() for r in out["results"]]}
        s = out["summary"]
        if s["downloaded"] + s["duplicate"] + s["skipped_exists"] == 0:
            return ok_envelope("download", data), C.EXIT_NO_RESULTS
        return ok_envelope("download", data), C.EXIT_OK
    finally:
        orch.close()


def cmd_inspect(args) -> tuple[dict, int]:
    from imgagent.images import looks_like_html, sniff_format
    from imgagent.sources import build_source
    from imgagent.sources.generic import GenericSource
    cfg = load_config(Path(getattr(args, "workspace", None)).expanduser() if getattr(args, "workspace", None) else None)
    orch = Orchestrator(cfg)
    try:
        url = args.url
        if "commons.wikimedia.org/wiki/File:" in url:
            result = build_source("wikimedia", orch.http, cfg).inspect(url)
            if result.candidate is not None:
                return ok_envelope("inspect", result.to_dict()), C.EXIT_OK
        result = GenericSource(orch.http, cfg).inspect(url)
        if result.kind == "image":
            # enrich direct image URLs with a bounded probe
            try:
                data, ctype, final_url = orch.http.fetch_bytes(url, args.max_probe_bytes, purpose="inspect")
                fmt = sniff_format(data)
                result.notes.append(f"probe: {len(data)} bytes, content-type={ctype}, "
                                    f"format={fmt or 'unrecognized'}")
                if looks_like_html(data):
                    result.notes.append("probe: payload looks like HTML")
            except ImgagentError as exc:
                result.notes.append(f"probe failed: {exc}")
        code = C.EXIT_OK if (result.candidate or result.page_candidates) else C.EXIT_NO_RESULTS
        return ok_envelope("inspect", result.to_dict()), code
    finally:
        orch.close()


def cmd_status(args) -> tuple[dict, int]:
    cfg = load_config(Path(getattr(args, "workspace", None)).expanduser() if getattr(args, "workspace", None) else None)
    ws = Workspace.from_config(cfg).ensure()
    orch = Orchestrator(cfg)
    try:
        data = orch.status()
        data["writable"] = ws.root.exists()
        if args.check_orphans:
            data["orphans"] = detect_orphans(ws, orch.db)
        if args.check_sources:
            from imgagent.sources import build_source
            checks = {}
            for name in cfg.sources:
                try:
                    src = build_source(name, orch.http, cfg)
                    checks[name] = bool(src.health_check())
                except Exception as exc:
                    checks[name] = False
                    data.setdefault("errors", []).append({"source": name, "error": str(exc)})
            data["source_health"] = checks
        code = C.EXIT_OK
        return ok_envelope("status", data), code
    finally:
        orch.close()


def cmd_agent_instructions(_args) -> tuple[dict, int]:
    return ok_envelope("agent-instructions", {"instructions": get_instructions()}), C.EXIT_OK


def cmd_mcp(_args) -> tuple[dict, int]:
    from imgagent.mcp_server import main as mcp_main
    return mcp_main() or C.EXIT_OK, C.EXIT_OK


_COMMANDS = {
    "run": cmd_run, "search": cmd_search, "list": cmd_list, "download": cmd_download,
    "inspect": cmd_inspect, "status": cmd_status, "agent-instructions": cmd_agent_instructions,
    "mcp": cmd_mcp,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    quiet = getattr(args, "quiet", False)

    if not args.command:
        parser.print_help(sys.stderr)
        return C.EXIT_USAGE

    if not quiet:
        log(f"[imgagent {__version__}] {args.command} starting")

    handler = _COMMANDS[args.command]
    try:
        if args.command == "mcp":
            code = handler(args)
            return code
        envelope, code = handler(args)
        if getattr(args, "json", False):
            emit_json(envelope)
        else:
            _print_human(args.command, envelope)
        if not quiet:
            status = "ok" if envelope.get("ok") else "failed"
            n_err = len(envelope.get("errors") or [])
            log(f"[imgagent] {args.command} finished: {status}" + (f" ({n_err} source error(s))" if n_err else ""))
        return code
    except (UsageError, ProviderError) as exc:
        return _emit_error(args, exc, C.EXIT_USAGE if isinstance(exc, UsageError) else C.EXIT_NO_RESULTS, quiet)
    except (ConfigError, DependencyError) as exc:
        return _emit_error(args, exc, C.EXIT_CONFIG, quiet)
    except NotFoundError as exc:
        return _emit_error(args, exc, C.EXIT_NO_RESULTS, quiet)
    except ImgagentError as exc:
        return _emit_error(args, exc, exc.exit_code, quiet)
    except BrokenPipeError:  # pragma: no cover
        return C.EXIT_OK
    except KeyboardInterrupt:  # pragma: no cover
        if getattr(args, "json", False):
            emit_json(err_envelope(args.command, "interrupted", "interrupted by user"))
        return C.EXIT_INTERRUPTED


def _emit_error(args, exc: ImgagentError, code: int, quiet: bool) -> int:
    if getattr(args, "json", False):
        emit_json(err_envelope(args.command, exc.kind, str(exc)))
    else:
        print(f"error[{exc.kind}]: {exc}", file=sys.stderr)
    if not quiet:
        log(f"[imgagent] {args.command} failed: {exc.kind}")
    return code


def _print_human(command: str, envelope: dict) -> None:
    data = envelope.get("data", {})
    errors = envelope.get("errors") or []
    if command == "agent-instructions":
        print(data.get("instructions", ""))
    elif command == "status":
        print_status_table(data)
    elif command in ("run", "search", "list") and "candidates" in data:
        cands = data.get("candidates") or []
        job = data.get("job_id") or data.get("job", {}).get("id")
        print(f"job: {job}  candidates: {data.get('total', len(cands))}")
        print_candidates_table(cands)
        if command == "run" and data.get("downloads"):
            print_downloads_table(data["downloads"])
    elif command == "download":
        print(f"job: {data.get('job_id')}  manifest: {data.get('manifest_path')}")
        print_downloads_table(data.get("downloads", {}))
        print_downloads_table_results(data.get("results") or [])
    elif command == "inspect":
        import json as _json
        print(_json.dumps(data, indent=2, ensure_ascii=False))
    if errors:
        print("source errors:", file=sys.stderr)
        for e in errors:
            print(f"  - {e.get('source')}: {e.get('error')}", file=sys.stderr)
