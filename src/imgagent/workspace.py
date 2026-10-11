"""Workspace layout and filesystem conventions.

.imgagent/
  config.toml
  jobs/
  database.sqlite
  manifests/
  cache/

assets/
  <project>/
    originals/
    processed/
    thumbnails/
    metadata.jsonl
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path

from imgagent.config import Config
from imgagent.util import now_utc_iso


@dataclass
class Workspace:
    root: Path            # .imgagent/
    db_path: Path
    jobs_dir: Path
    manifests_dir: Path
    cache_dir: Path
    assets_root: Path

    @classmethod
    def from_config(cls, cfg: Config) -> Workspace:
        root = cfg.workspace
        return cls(
            root=root,
            db_path=root / "database.sqlite",
            jobs_dir=root / "jobs",
            manifests_dir=root / "manifests",
            cache_dir=root / "cache",
            assets_root=cfg.assets_root,
        )

    def ensure(self) -> Workspace:
        for p in (self.root, self.jobs_dir, self.manifests_dir, self.cache_dir):
            p.mkdir(parents=True, exist_ok=True)
        return self

    def project_dir(self, project: str) -> Path:
        safe = project.strip().replace("/", "_").replace("\\", "_") or "default"
        d = self.assets_root / safe
        (d / "originals").mkdir(parents=True, exist_ok=True)
        (d / "processed").mkdir(parents=True, exist_ok=True)
        (d / "thumbnails").mkdir(parents=True, exist_ok=True)
        return d

    def metadata_jsonl(self, project: str) -> Path:
        return self.project_dir(project) / "metadata.jsonl"

    def check_writable(self) -> None:
        """Detect read-only or missing persistent storage before work is lost."""
        try:
            probe = self.root / ".write-probe"
            probe.write_text("ok")
            probe.unlink()
        except OSError as exc:
            from imgagent.errors import ConfigError
            raise ConfigError(
                f"workspace {self.root} is not writable ({exc}). "
                "For read-only filesystems set IMGAGENT_WORKSPACE (or --workspace) to a writable path."
            ) from exc


class MetadataLog:
    """Thread-safe appender for assets/<project>/metadata.jsonl."""

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: dict) -> None:
        line = json.dumps({"recorded_at": now_utc_iso(), **record}, ensure_ascii=False)
        with self._lock:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def read_all(self) -> list[dict]:
        if not self._path.is_file():
            return []
        out = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out


def find_workspace(start: Path | None = None) -> Path | None:
    """Walk up from `start` (default cwd) looking for an existing .imgagent directory."""
    cur = (start or Path.cwd()).resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / ".imgagent").is_dir():
            return candidate / ".imgagent"
    return None
