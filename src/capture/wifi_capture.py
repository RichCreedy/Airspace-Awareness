"""
Passive WiFi Remote ID sniffer using scapy on a monitor-mode interface.
Parses ASTM F3411 vendor-specific IE.
"""
import threading
import logging
from scapy.all import sniff, Dot11Elt, Dot11

from src.remoteid.parser import parse_remoteid_message

log = logging.getLogger(__name__)

# TODO: confirm OUI bytes for Remote ID vendor IE (ASTM/ASD-STAN = 0xFA0BBC)
REMOTEID_OUI = b"\xfa\x0b\xbc"


class WifiCapture:
    def __init__(self, interface, on_track=None):
        self.interface = interface
        self.on_track = on_track
        self._thread = None
        self._running = False

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _run(self):
        try:
            sniff(
                iface=self.interface,
                prn=self._handle_packet,
                store=False,
                stop_filter=lambda p: not self._running,
            )
        except Exception as e:
            log.exception("WiFi sniff failed on %s: %s", self.interface, e)

    def _handle_packet(self, pkt):
        if not pkt.haslayer(Dot11Elt):
            return
        elt = pkt.getlayer(Dot11Elt)
        while elt:
            if elt.ID == 221 and elt.info.startswith(REMOTEID_OUI):
                try:
                    track = parse_remoteid_message(elt.info[3:])
                    if track and self.on_track:
                        track["source"] = "wifi"
                        self.on_track(track)
                except Exception as e:
                    log.debug("Parse failure: %s", e)
            elt = elt.payload.getlayer(Dot11Elt)
