"""
modules/gps_reader.py — Background gpsd client feeding FusionEngine

Requires gpsd running and configured against /dev/ttyAMA0 @ 9600 baud, e.g.
in /etc/default/gpsd:
    DEVICES="/dev/ttyAMA0"
    GPSD_OPTIONS="-n -s 9600"
"""

from __future__ import annotations

import select
import logging
import threading
import time
from typing import Callable, Optional

import gps  # python3-gps

LOG = logging.getLogger("gps_reader")


class GPSReader:
    def __init__(self, config: dict, on_fix: Callable[[dict], None]):
        gcfg = config.get("gps", {})
        self.host = gcfg.get("gpsd_host", "localhost")
        self.port = gcfg.get("gpsd_port", 2947)
        self.reconnect_delay_s = gcfg.get("reconnect_delay_s", 5)
        self.poll_timeout_s = gcfg.get("poll_timeout_s", 0.5)

        self.on_fix = on_fix

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._session: Optional[gps.gps] = None

        self._status = {
            "connected": False,
            "last_fix_ts": None,
            "last_error": None,
            "error_count": 0,
        }
        self._status_lock = threading.Lock()

    # ------------------------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="gps_reader", daemon=True)
        self._thread.start()
        LOG.info("GPSReader started (gpsd at %s:%s)", self.host, self.port)

    def stop(self):
        self._stop_event.set()
        if self._session:
            try:
                self._session.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=3)

    def get_status(self) -> dict:
        with self._status_lock:
            return dict(self._status)

    # ------------------------------------------------------------------
    def _run(self):
        while not self._stop_event.is_set():
            try:
                self._connect_and_poll()
            except Exception as exc:
                self._record_error(str(exc))
                LOG.warning("GPSReader connection error: %s — retrying in %ss",
                            exc, self.reconnect_delay_s)
            finally:
                with self._status_lock:
                    self._status["connected"] = False
            self._stop_event.wait(self.reconnect_delay_s)

    def _connect_and_poll(self):
        self._session = gps.gps(host=self.host, port=str(self.port),
                                 mode=gps.WATCH_ENABLE | gps.WATCH_NEWSTYLE)
        with self._status_lock:
            self._status["connected"] = True
            self._status["last_error"] = None
        LOG.info("Connected to gpsd")

        while not self._stop_event.is_set():
            readable, _, _ = select.select([self._session.sock], [], [],
                                            self.poll_timeout_s)
            if not readable:
                continue

            report = self._session.next()
            if report is None:
                continue

            if getattr(report, "class", None) == "TPV":
                self._handle_tpv(report)

    def _handle_tpv(self, report):
        mode = getattr(report, "mode", 0)
        if mode < 2:
            return  # no usable fix yet

        fix = {
            "lat": getattr(report, "lat", None),
            "lon": getattr(report, "lon", None),
            "alt_m": getattr(report, "altMSL", getattr(report, "alt", None)),
            "speed_mps": getattr(report, "speed", None),
            "track_deg": getattr(report, "track", None),
            "mode": mode,
            "time": getattr(report, "time", None),
        }

        if fix["lat"] is None or fix["lon"] is None:
            return

        with self._status_lock:
            self._status["last_fix_ts"] = time.time()

        try:
            self.on_fix(fix)
        except Exception:
            LOG.exception("on_fix callback raised")

    def _record_error(self, msg: str):
        with self._status_lock:
            self._status["last_error"] = msg
            self._status["error_count"] += 1
