"""
Polls tar1090's aircraft.json for ADS-B tracks.
"""
import threading
import time
import logging
import requests

log = logging.getLogger(__name__)


class ADSBCapture:
    def __init__(self, endpoint, poll_interval_sec=2, on_update=None):
        self.endpoint = endpoint
        self.poll_interval_sec = poll_interval_sec
        self.on_update = on_update
        self._running = False
        self._thread = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _run(self):
        while self._running:
            try:
                resp = requests.get(self.endpoint, timeout=3)
                resp.raise_for_status()
                data = resp.json()
                aircraft = data.get("aircraft", [])
                tracks = [self._normalize(a) for a in aircraft if "lat" in a and "lon" in a]
                if self.on_update:
                    self.on_update(tracks)
            except Exception as e:
                log.warning("ADS-B poll failed: %s", e)
            time.sleep(self.poll_interval_sec)

    @staticmethod
    def _normalize(a):
        return {
            "id": a.get("hex"),
            "source": "adsb",
            "lat": a.get("lat"),
            "lon": a.get("lon"),
            "alt": a.get("alt_baro"),
            "heading": a.get("track"),
            "speed": a.get("gs"),
            "callsign": a.get("flight", "").strip(),
            "ts": time.time(),
        }
