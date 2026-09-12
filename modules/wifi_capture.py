"""
modules/wifi_capture.py — Passive WiFi Remote ID sniffer

Requires the configured interface to already be in monitor mode, e.g.:
    sudo ip link set wlan1 down
    sudo iw wlan1 set monitor none
    sudo ip link set wlan1 up
(handled by setup scripts / systemd unit, not by this module)

ASTM F3411 / OpenDroneID WiFi Beacon method: vendor-specific IE (element
ID 0xDD) with OUI + vendor type identifying it as a Remote ID message.

⚠️ VERIFY: OUI bytes below (FA:0B:BC, vendor type 0x0D) follow the
OpenDroneID project convention as commonly implemented, but should be
confirmed against a real capture from a known Remote ID-broadcasting
drone before relying on this in the field. Flagged, not asserted as fact.
"""

from __future__ import annotations

import time
import logging
import threading
from typing import Callable, Optional

from scapy.all import sniff
from scapy.layers.dot11 import Dot11, Dot11Beacon, Dot11ProbeResp, Dot11Elt

from modules.remoteid_parser import parse_remoteid_payload

LOG = logging.getLogger("wifi_capture")

ASTM_OUI = b"\xfa\x0b\xbc"      # ⚠️ verify against real capture
REMOTEID_VENDOR_TYPE = 0x0D     # ⚠️ verify against real capture
VENDOR_SPECIFIC_ELEMENT_ID = 221  # 0xDD


class WiFiCapture:
    def __init__(self, config: dict, on_detection: Callable[[dict], None]):
        wcfg = config.get("wifi", {})
        self.interface = wcfg.get("interface", "wlan1mon")
        self.burst_s = wcfg.get("sniff_burst_s", 1)

        self.on_detection = on_detection

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

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
        if not self._check_monitor_mode():
            LOG.error(
                "Interface %s does not appear to be in monitor mode — "
                "Remote ID detection will not work. Check setup.", self.interface
            )
            with self._status_lock:
                self._status["last_error"] = f"{self.interface} not in monitor mode"

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="wifi_capture", daemon=True)
        self._thread.start()
        with self._status_lock:
            self._status["running"] = True
        LOG.info("WiFiCapture started on %s", self.interface)

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3)
        with self._status_lock:
            self._status["running"] = False

    def get_status(self) -> dict:
        with self._status_lock:
            return dict(self._status)

    # ------------------------------------------------------------------
    def _check_monitor_mode(self) -> bool:
        """Best-effort check via /sys — does not attempt to fix, only warns."""
        try:
            with open(f"/sys/class/net/{self.interface}/type") as f:
                iftype = f.read().strip()
            # ARPHRD_IEEE80211_RADIOTAP == 803 indicates monitor mode w/ radiotap
            return iftype == "803"
        except Exception:
            LOG.debug("Could not verify monitor mode for %s via sysfs", self.interface)
            return True  # don't block startup on an inconclusive check

    def _run(self):
        while not self._stop_event.is_set():
            try:
                sniff(
                    iface=self.interface,
                    prn=self._handle_packet,
                    timeout=self.burst_s,
                    store=False,
                )
            except Exception as exc:
                with self._status_lock:
                    self._status["last_error"] = str(exc)
                LOG.warning("WiFiCapture sniff error on %s: %s", self.interface, exc)
                self._stop_event.wait(2)  # avoid tight error loop

    def _handle_packet(self, pkt):
        if not pkt.haslayer(Dot11Elt):
            return
        if not (pkt.haslayer(Dot11Beacon) or pkt.haslayer(Dot11ProbeResp)):
            return

        mac = pkt.getlayer(Dot11).addr2 if pkt.haslayer(Dot11) else None

        elt = pkt.getlayer(Dot11Elt)
        while elt:
            if elt.ID == VENDOR_SPECIFIC_ELEMENT_ID:
                raw = bytes(elt.info)
                if len(raw) >= 4 and raw[0:3] == ASTM_OUI and raw[3] == REMOTEID_VENDOR_TYPE:
                    self._parse_and_dispatch(raw[4:], mac)
            elt = elt.payload.getlayer(Dot11Elt) if elt.payload else None

    def _parse_and_dispatch(self, payload: bytes, mac: Optional[str]):
        try:
            parsed = parse_remoteid_payload(payload)
        except Exception:
            LOG.exception("remoteid_parser raised on WiFi payload (len=%d)", len(payload))
            return

        if parsed is None:
            LOG.debug("WiFi payload did not parse as valid Remote ID (len=%d)", len(payload))
            return

        detection = dict(parsed)
        detection["source"] = "wifi"
        detection.setdefault("basic_id", mac)  # fallback key if parser found no basic_id

        with self._status_lock:
            self._status["last_detection_ts"] = time.time()
            self._status["detection_count"] += 1

        try:
            self.on_detection(detection)
        except Exception:
            LOG.exception("on_detection callback raised")
