"""
Tracks last-sync timestamps and errors per data source.
Backs the freshness badge widget.
"""
import json
import os
import time
import threading
import logging

log = logging.getLogger(__name__)

DEFAULT_PATH = "data/sync_metadata.json"


class SyncMetadata:
    def __init__(self, path=DEFAULT_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._data = {}
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    self._data = json.load(f)
            except Exception:
                self._data = {}

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self._data, f, indent=2)

    def record_success(self, source):
        with self._lock:
            self._data[source] = {
                "last_success": time.time(),
                "last_error": None,
            }
            self._save()

    def record_failure(self, source, error_msg):
        with self._lock:
            entry = self._data.get(source, {})
            entry["last_error"] = {"message": error_msg, "ts": time.time()}
            self._data[source] = entry
            self._save()

    def get_status(self, source):
        with self._lock:
            return dict(self._data.get(source, {}))

    def get_all(self):
        with self._lock:
            return dict(self._data)

    def is_stale(self, source, max_age_sec):
        status = self.get_status(source)
        last = status.get("last_success")
        if last is None:
            return True
        return (time.time() - last) > max_age_sec
