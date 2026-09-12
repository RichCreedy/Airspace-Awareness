"""
GPS reader — connects to gpsd, exposes latest fix.
Device: /dev/ttyAMA0 @ 9600 baud (Ai-Thinker GP-02).
"""
import threading
import time
import logging

try:
    import gps
except ImportError:
    gps = None

log = logging.getLogger(__name__)


class GPSReader:
    def __init__(self, use_gpsd=True):
        self.use_gpsd = use_gpsd
        self._lock = threading.Lock()
        self._fix = {"lat": None, "lon": None, "alt": None, "status": "NO_FIX", "ts": None}
        self._thread = None
        self._running = False

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _run(self):
        if gps is None:
            log.error("python3-gps not installed; GPS disabled.")
            return
        session = gps.gps(mode=gps.WATCH_ENABLE)
        while self._running:
            try:
                report = session.next()
                if report["class"] == "TPV":
                    lat = getattr(report, "lat", None)
                    lon = getattr(report, "lon", None)
                    alt = getattr(report, "alt", None)
                    if lat is not None and lon is not None:
                        with self._lock:
                            self._fix = {
                                "lat": lat, "lon": lon, "alt": alt,
                                "status": "FIX", "ts": time.time(),
                            }
            except StopIteration:
                log.warning("gpsd connection closed, retrying...")
                time.sleep(2)
                session = gps.gps(mode=gps.WATCH_ENABLE)
            except Exception as e:
                log.exception("GPS read error: %s", e)
                time.sleep(2)

    def get_fix(self):
        with self._lock:
            return dict(self._fix)
