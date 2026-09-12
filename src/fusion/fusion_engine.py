"""
Fusion engine — merges tracks from ADS-B, WiFi, Bluetooth.
Prunes stale entries.
"""
import time
import threading
import logging

log = logging.getLogger(__name__)


class FusionEngine:
    def __init__(self, stale_timeout_sec=30):
        self.stale_timeout_sec = stale_timeout_sec
        self._lock = threading.Lock()
        self._tracks = {}  # id -> track dict

    def update_track(self, track):
        if not track or not track.get("id"):
            return
        with self._lock:
            track["ts"] = track.get("ts", time.time())
            existing = self._tracks.get(track["id"], {})
            existing.update(track)
            self._tracks[track["id"]] = existing

    def update_many(self, tracks):
        for t in tracks:
            self.update_track(t)

    def prune_stale(self):
        now = time.time()
        with self._lock:
            stale = [
                tid for tid, t in self._tracks.items()
                if now - t.get("ts", 0) > self.stale_timeout_sec
            ]
            for tid in stale:
                del self._tracks[tid]
        if stale:
            log.debug("Pruned %d stale tracks", len(stale))

    def get_all_tracks(self):
        with self._lock:
            return list(self._tracks.values())

    def icon_for(self, track):
        source = track.get("source")
        if source == "adsb":
            return "images/icons/plane_blue.png"
        if source == "wifi":
            return "images/icons/drone_orange.png"
        if source == "bluetooth":
            return "images/icons/drone_purple.png"
        return "images/icons/unknown_grey.png"
