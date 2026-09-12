"""
Passive Bluetooth Remote ID scanner using Bleak.
Parses Service Data UUID 0xFFFA.
"""
import asyncio
import threading
import logging
from bleak import BleakScanner

from src.remoteid.parser import parse_remoteid_message

log = logging.getLogger(__name__)

SERVICE_UUID = "0000fffa-0000-1000-8000-00805f9b34fb"


class BluetoothCapture:
    def __init__(self, scan_timeout_sec=5, on_track=None):
        self.scan_timeout_sec = scan_timeout_sec
        self.on_track = on_track
        self._running = False
        self._thread = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _run_loop(self):
        asyncio.run(self._scan_forever())

    async def _scan_forever(self):
        while self._running:
            try:
                devices = await BleakScanner.discover(timeout=self.scan_timeout_sec)
                for d in devices:
                    sd = d.metadata.get("service_data", {}) if d.metadata else {}
                    raw = sd.get(SERVICE_UUID)
                    if raw:
                        try:
                            track = parse_remoteid_message(raw)
                            if track and self.on_track:
                                track["source"] = "bluetooth"
                                self.on_track(track)
                        except Exception as e:
                            log.debug("BT parse failure: %s", e)
            except Exception as e:
                log.warning("BT scan error: %s", e)
                await asyncio.sleep(2)
