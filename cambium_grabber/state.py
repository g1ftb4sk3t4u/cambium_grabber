"""Persistent state: which products/files we've already got, written atomically.

Adapted from the MikroTik grabber's Manifest - same mechanics (single JSON
file, write-to-temp-then-replace, skip-if-already-downloaded), just keyed by
"group" (a product/category id like "PMP 450" or "cnPilot E410") instead of
a RouterOS version string, since Cambium's downloads are organized by
product line and file category rather than one flat version number.
"""

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path


class Manifest:
    """Single JSON manifest tracking discovered product groups, downloaded
    files, and stats. Written via write-to-temp-then-replace so a crash
    mid-write can't corrupt it.
    """

    def __init__(self, output_dir: Path):
        self.path = Path(output_dir) / "manifest.json"
        self._lock = threading.Lock()
        self.data = {
            "groups": {},   # group_id -> {"discovered_at": iso, "files": {filename: {size, path, url}}}
            "stats": {
                "groups_found": 0,
                "files_downloaded": 0,
                "files_skipped": 0,
                "files_failed": 0,
                "bytes_downloaded": 0,
            },
            "last_full_scan": None,
            "last_watch_check": None,
        }
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                self.data.update(loaded)
            except (json.JSONDecodeError, OSError):
                pass

    def save(self):
        with self._lock:
            tmp_path = self.path.with_suffix(".json.tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp_path, self.path)

    def has_group(self, group_id: str) -> bool:
        return group_id in self.data["groups"]

    def record_group(self, group_id: str):
        with self._lock:
            if group_id not in self.data["groups"]:
                self.data["groups"][group_id] = {
                    "discovered_at": _now(),
                    "files": {},
                }
                self.data["stats"]["groups_found"] += 1

    def has_file(self, group_id: str, filename: str, expected_size: int = None) -> bool:
        entry = self.data["groups"].get(group_id, {}).get("files", {}).get(filename)
        if not entry:
            return False
        if expected_size is not None and entry.get("size") != expected_size:
            return False
        return True

    def record_file(self, group_id: str, filename: str, size: int, rel_path: str, source_url: str = None):
        with self._lock:
            self.data["groups"].setdefault(group_id, {"discovered_at": _now(), "files": {}})
            self.data["groups"][group_id]["files"][filename] = {
                "size": size,
                "path": rel_path,
                "url": source_url,
                "downloaded_at": _now(),
            }

    def bump_stat(self, key: str, amount=1):
        with self._lock:
            self.data["stats"][key] = self.data["stats"].get(key, 0) + amount

    def mark_full_scan(self):
        with self._lock:
            self.data["last_full_scan"] = _now()

    def mark_watch_check(self):
        with self._lock:
            self.data["last_watch_check"] = _now()

    def known_groups(self):
        return set(self.data["groups"].keys())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
