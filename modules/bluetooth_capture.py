"""
modules/bluetooth_capture.py — Passive Bluetooth Remote ID scanner (Bleak)

ASTM F3411 Bluetooth Legacy Advertising method: Remote ID payload carried
in Service Data with 16-bit UUID 0xFFFA, expanded to
0000fffa-0000-1000-8000-00805f9b34fb (Bluetooth base UUID).

Runs Bleak (asyncio) inside its own thread + event loop so the rest of
the app can treat this module with the same start()/stop() lifecycle as
the thread-based WiFi/GPS/ADS-B modules, without requiring main.py to be
asyncio-aware.
"""

from __future__ import annotations

import time
import asyncio
import logging
import threading
from typing import Callable, Optional

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from modules.remoteid_parser import parse_remoteid_payload

LOG = logging.getLogger("bluetooth_capture")

REMOTEID_SERVICE_UUID = "0000fffa-0000-1000-8000-00805f9b34fb"


class BluetoothCapture:
    def __init__(self, config: dict, on_detection: Callable[[dict], None]):
        bcfg = config.get("bluetooth", {})
        self.adapter = bcfg.get("adapter", None)  # None = Bleak default
        self.scan_restart_s = bcfg.get("scan_restart_s", 30)  # periodic scanner refresh

        self.on_detection = on_detection

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        self._status = {
            "running": False,
            "last_detection_ts": None,
            "detection_count": 0,
            "last_error": None,
        }
        self._status_lock = threading.Lock()

    # ------------------------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="bluetooth_capture", daemon=True)
        self._thread.start()
        LOG.info("BluetoothCapture started (adapter=%s)", self.adapter or "default")

    def stop(self):
        self._stop_event.set()
        if self._loop and self._loop.is_running():
            # Wake the loop so it notices the stop_event promptly
            asyncio.run_coroutine_threadsafe(self._noop(), self._loop)
        if self._thread:
            self._thread.join(timeout=5)
        with self._status_lock:
            self._status["running"] = False

    def get_status(self) -> dict:
        with self._status_lock:
            return dict(self._status)

    async def _noop(self):
        return

    # ------------------------------------------------------------------
    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        with self._status_lock:
            self._status["running"] = True
        try:
            self._loop.run_until_complete(self._scan_forever())
        except Exception:
            LOG.exception("BluetoothCapture event loop crashed")
        finally:
            self._loop.close()

    async def _scan_forever(self):
        """
        Restarts the scanner periodically — Bleak scanners on some
        BlueZ versions have been observed to silently stop delivering
        callbacks after long uptimes; a periodic restart is a cheap
        mitigation rather than assuming indefinite reliability.
        """
        while not self._stop_event.is_set():
            try:
                scanner = BleakScanner(
                    detection_callback=self._on_advertisement,
                    adapter=self.adapter,
                )
                await scanner.start()
                LOG.info("BLE scanner active")

                elapsed = 0
                while not self._stop_event.is_set() and elapsed < self.scan_restart_s:
                    await asyncio.sleep(1)
                    elapsed += 1

                await scanner.stop()

            except Exception as exc:
                with self._status_lock:
                    self._status["last_error"] = str(exc)
                LOG.warning("BLE scan error: %s — retrying in 5s", exc)
                await asyncio.sleep(5)

    def _on_advertisement(self, device: BLEDevice, adv: AdvertisementData):
        service_data = adv.service_data or {}
        raw = service_data.get(REMOTEID_SERVICE_UUID)
        if raw is None:
            return

        try:
            parsed = parse_remoteid_payload(bytes(raw))
        except Exception:
            LOG.exception("remoteid_parser raised on BT payload (len=%d)", len(raw))
            return

        if parsed is None:
            LOG.debug("BT payload did not parse as valid Remote ID (len=%d)", len(raw))
            return

        detection = dict(parsed)
        detection["source"] = "bluetooth"
        detection.setdefault("basic_id", device.address)  # fallback key

        with self._status_lock:
            self._status["last_detection_ts"] = time.time()
            self._status["detection_count"] += 1

        try:
            self.on_detection(detection)
        except Exception:
            LOG.exception("on_detection callback raised")
