"""Human-readable tables (stderr stays log-only; these print to stdout)."""

from __future__ import annotations

import shutil


def _col(text, width: int) -> str:
    s = str(text if text is not None else "-")
    return s[: width - 1] + "…" if len(s) >= width else s.ljust(width)


def print_candidates_table(candidates: list[dict]) -> None:
    if not candidates:
        print("  (no candidates)")
        return
    term = shutil.get_terminal_size((120, 24)).columns
    widths = {"idx": 4, "score": 5, "prov": 10, "rights": 24, "dims": 11, "id": 14}
    title_w = max(20, term - sum(widths.values()) - 40)
    print(f"{_col('idx',4)} {_col('score',5)} {_col('provider',10)} {_col('rights_status',24)} "
          f"{_col('dims',11)} {_col('candidate_id',14)} {_col('title', title_w)}")
    for i, c in enumerate(candidates):
        dims = f"{c.get('width') or '?'}x{c.get('height') or '?'}"
        print(f"{_col(i,4)} {_col(round(c.get('relevance_score') or 0, 1),5)} "
              f"{_col(c.get('provider'),10)} {_col(c.get('rights_status'),24)} "
              f"{_col(dims,11)} {_col(c.get('candidate_id'),14)} {_col(c.get('title'), title_w)}")


def print_downloads_table(summary: dict) -> None:
    if not summary:
        return
    print(f"downloads: attempted={summary.get('attempted', 0)} downloaded={summary.get('downloaded', 0)} "
          f"duplicate={summary.get('duplicate', 0)} skipped={summary.get('skipped_exists', 0)} "
          f"failed={summary.get('failed', 0)}")


def print_downloads_table_results(results: list[dict]) -> None:
    for r in results:
        if r.get("status") == "failed":
            print(f"  FAILED {r.get('candidate_id')} ({r.get('error_kind')}): {r.get('error')}")
        elif r.get("path"):
            print(f"  {r.get('status'):>15} {r.get('path')}")


def print_status_table(data: dict) -> None:
    print(f"workspace : {data.get('workspace')}")
    print(f"assets    : {data.get('assets_root')}")
    counts = data.get("counts", {})
    print(f"counts    : jobs={counts.get('jobs', 0)} candidates={counts.get('candidates', 0)} "
          f"assets={counts.get('assets', 0)}")
    storage = data.get("storage", {})
    from imgagent.util import human_bytes as hb
    print(f"storage   : assets={hb(storage.get('assets_bytes', 0))} "
          f"database={hb(storage.get('database_bytes', 0))} cache_files={storage.get('cache_files', 0)}")
    print(f"sources   : {', '.join(data.get('sources_enabled', []))}")
    if "source_health" in data:
        for name, healthy in data["source_health"].items():
            print(f"  {name:<14} {'healthy' if healthy else 'UNAVAILABLE'}")
    if "orphans" in data:
        print(f"orphans   : {len(data['orphans'])}")
