"""
modules/adsb_ingest.py — Polls tar1090 aircraft.json, feeds FusionEngine

tar1090 default endpoint when served via lighttpd on this hardware is
typically http://localhost/tar1090/data/aircraft.json — confirmed reachable
per earlier testing. Configurable in case of a different mount path.
"""

from __future__ import annotations

import time
import logging
import threading
from typing import Callable, Optional

import requests

LOG = logging.getLogger("adsb_ingest")


class ADSBIngest:
    def __init__(self, config: dict, on_update: Callable[[list[dict]], None]):
        acfg = config.get("adsb", {})
        self.url = acfg.get("url", "http://localhost/tar1090/data/aircraft.json")
        self.poll_interval_s = acfg.get("poll_interval_s", 2)
        self.timeout_s = acfg.get("timeout_s", 3)

        self.backoff_base_s = acfg.get("backoff_base_s", 2)
        self.backoff_max_s = acfg.get("backoff_max_s", 60)

        self.on_update = on_update

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._consecutive_failures = 0

        self._status = {
            "last_success_ts": None,
            "last_error": None,
            "consecutive_failures": 0,
            "current_delay_s": self.poll_interval_s,
            "last_aircraft_count": 0,
        }
        self._status_lock = threading.Lock()

    # ------------------------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="adsb_ingest", daemon=True)
        self._thread.start()
        LOG.info("ADSBIngest started (url=%s, interval=%ss)", self.url, self.poll_interval_s)

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3)

    def get_status(self) -> dict:
        with self._status_lock:
            return dict(self._status)

    # ------------------------------------------------------------------
    def _run(self):
        while not self._stop_event.is_set():
            delay = self._poll_once()
            self._stop_event.wait(delay)

    def _poll_once(self) -> float:
        """Performs one fetch. Returns the delay to wait before the next poll."""
        try:
            resp = requests.get(self.url, timeout=self.timeout_s)
            resp.raise_for_status()
            data = resp.json()
            aircraft = data.get("aircraft", [])

            self._consecutive_failures = 0
            with self._status_lock:
                self._status["last_success_ts"] = time.time()
                self._status["last_error"] = None
                self._status["consecutive_failures"] = 0
                self._status["current_delay_s"] = self.poll_interval_s
                self._status["last_aircraft_count"] = len(aircraft)

            try:
                self.on_update(aircraft)
            except Exception:
                LOG.exception("on_update callback raised")

            return self.poll_interval_s

        except Exception as exc:
            self._consecutive_failures += 1
            delay = min(
                self.backoff_base_s * (2 ** (self._consecutive_failures - 1)),
                self.backoff_max_s,
            )
            with self._status_lock:
                self._status["last_error"] = str(exc)
                self._status["consecutive_failures"] = self._consecutive_failures
                self._status["current_delay_s"] = delay

            LOG.warning("ADS-B poll failed (%d consecutive): %s — retrying in %ss",
                        self._consecutive_failures, exc, delay)
            return delay
