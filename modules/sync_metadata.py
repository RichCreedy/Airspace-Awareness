"""
sync_metadata.py

Tracks last-sync status (success/failure/timestamps) for each airspace
data source (OpenAIP, NOTAM import, FRZ generation, GPS). Persisted to a
small JSON file so status survives app restarts.

Used by:
  - gui/freshness_badge.py   (to show last sync time / staleness)
  - modules/auto_sync_scheduler.py (backoff decisions)
"""
import json
import os
import threading
from datetime import datetime, timezone

DEFAULT_METADATA_PATH = "data/sync_metadata.json"


class SyncMetadata:
    def __init__(self, path=DEFAULT_METADATA_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._data = {}
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r") as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._data = {}
        else:
            self._data = {}

    def _save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp_path = self.path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(self._data, f, indent=2)
        os.replace(tmp_path, self.path)

    def record_success(self, source, detail=None):
        with self._lock:
            self._data[source] = {
                "last_success": datetime.now(timezone.utc).isoformat(),
                "last_attempt": datetime.now(timezone.utc).isoformat(),
                "last_error": None,
                "detail": detail,
            }
            self._save()

    def record_failure(self, source, error):
        with self._lock:
            entry = self._data.get(source, {})
            entry["last_attempt"] = datetime.now(timezone.utc).isoformat()
            entry["last_error"] = str(error)
            entry.setdefault("last_success", None)
            self._data[source] = entry
            self._save()

    def get(self, source):
        with self._lock:
            return self._data.get(source, {
                "last_success": None,
                "last_attempt": None,
                "last_error": None,
                "detail": None,
            })

    def get_all(self):
        with self._lock:
            return dict(self._data)

    def get_age_hours(self, source):
        entry = self.get(source)
        last_success = entry.get("last_success")
        if not last_success:
            return None
        try:
            ts = datetime.fromisoformat(last_success)
        except ValueError:
            return None
        delta = datetime.now(timezone.utc) - ts
        return delta.total_seconds() / 3600.0

    def is_stale(self, source, max_age_hours):
        age = self.get_age_hours(source)
        return True if age is None else age > max_age_hours

    def has_error(self, source):
        return self.get(source).get("last_error") is not None
