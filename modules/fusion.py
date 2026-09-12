"""
modules/fusion.py — Thread-safe shared-state fusion hub

Central point where all sensor/data sources converge:
    - GPS fix (ownship position)
    - ADS-B tracks (from modules/adsb_ingest.py, polling tar1090's
      aircraft.json)
    - Remote ID tracks from passive WiFi capture (modules/wifi_capture.py)
      and Bluetooth capture (modules/bt_capture.py) — merged by
      `basic_id` when both sources report the same drone
    - Geofence/NOTAM/FRZ zones (from modules/airspace_manager.py)
    - Proximity alerts (from modules/proximity_alert.py)

Design notes:
    - All mutating/reading methods are protected by a single RLock.
      This module is intentionally simple/coarse-grained rather than
      lock-per-field, since update frequency is low (~1-10 Hz) relative
      to typical GUI/CPU budgets on uConsole hardware.
    - A background thread periodically prunes stale tracks based on
      per-source timeouts (start()/stop() control this thread).
    - get_snapshot() is the single method the GUI render loop should
      call once per frame — it returns a fully-merged, position-filtered,
      icon-resolved view of the world, so gui/app.py does not need to
      know about merging logic at all.

ASSUMPTIONS (flagged — confirm against actual capture module output):
    - ADS-B tracks arrive via update_adsb_tracks(list_of_dicts), each
      dict having at minimum: {"icao": str, "lat": float, "lon": float}
      and optionally "callsign", "altitude_m", "heading", "speed_mps".
      This is an upsert (not replace) — entries not present in a given
      call remain until they age out via ADSB_TIMEOUT_S. This matches
      tar1090 polling behaviour (a single poll glitch shouldn't cause a
      track to vanish and reappear).
    - Remote ID tracks arrive via update_remoteid(source, track), where
      source is "wifi" or "bluetooth", and track has at minimum:
      {"track_key": str} (a stable per-session identifier — e.g. MAC
      address) and optionally "basic_id" (str, the ASTM F3411 UAS ID —
      once decoded), "lat", "lon", "altitude_m", "heading", "speed_mps".
      Merging across wifi+bluetooth ONLY happens when both sides report
      the same non-empty basic_id. Until a basic_id is decoded, tracks
      are tracked individually by (source, track_key).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

LOG = logging.getLogger("modules.fusion")

# ----------------------------------------------------------------------
# Icon constants
# ----------------------------------------------------------------------
ICON_PLANE = "images/icons/plane_blue.png"
ICON_DRONE_CROSS_VERIFIED = "images/icons/drone_orange.png"   # wifi + bt agree on basic_id
ICON_DRONE_SINGLE_SOURCE = "images/icons/drone_purple.png"     # basic_id known, one source
ICON_UNKNOWN = "images/icons/unknown_grey.png"                 # no confirmed identity
ICON_OWNSHIP = "images/icons/ownship.png"                      # for GUI's own use

# ----------------------------------------------------------------------
# Default per-source timeouts / intervals
# ----------------------------------------------------------------------
DEFAULT_TIMEOUTS_S = {
    "adsb": 60.0,
    "remoteid": 45.0,
    "gps": 15.0,
}
DEFAULT_PRUNE_INTERVAL_S = 5.0


def _monotonic() -> float:
    return time.monotonic()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Fusion:
    def __init__(self, timeouts: Optional[Dict[str, float]] = None,
                 prune_interval_s: float = DEFAULT_PRUNE_INTERVAL_S):
        self._lock = threading.RLock()

        self._timeouts = dict(DEFAULT_TIMEOUTS_S)
        if timeouts:
            self._timeouts.update(timeouts)
        self._prune_interval_s = prune_interval_s

        # --- ADS-B: keyed by icao ---
        self._adsb: Dict[str, dict] = {}

        # --- Remote ID: keyed by track_key, one dict per source ---
        self._remoteid_wifi: Dict[str, dict] = {}
        self._remoteid_bt: Dict[str, dict] = {}

        # --- GPS fix (single latest value) ---
        self._gps_fix: Optional[dict] = None

        # --- Zones / alerts (pushed wholesale by other modules) ---
        self._zones: List[dict] = []
        self._alerts: List[dict] = []

        # --- Background pruning thread control ---
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self):
        """Start the background pruning thread. Idempotent."""
        if self._thread and self._thread.is_alive():
            LOG.warning("Fusion.start() called but pruning thread already running")
            return
        LOG.info("Starting fusion pruning thread (interval=%.1fs)", self._prune_interval_s)
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._prune_loop, name="fusion-prune", daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the background pruning thread and wait for it to exit."""
        LOG.info("Stopping fusion pruning thread")
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                LOG.warning("Fusion pruning thread did not stop within timeout")

    def _prune_loop(self):
        while not self._stop_event.is_set():
            try:
                self._prune_once()
            except Exception:
                LOG.exception("Error during fusion prune cycle")
            self._stop_event.wait(timeout=self._prune_interval_s)

    def _prune_once(self):
        now = _monotonic()
        with self._lock:
            self._prune_dict(self._adsb, now, self._timeouts["adsb"], "ADS-B")
            self._prune_dict(self._remoteid_wifi, now, self._timeouts["remoteid"], "RemoteID/WiFi")
            self._prune_dict(self._remoteid_bt, now, self._timeouts["remoteid"], "RemoteID/BT")

    @staticmethod
    def _prune_dict(store: Dict[str, dict], now: float, timeout_s: float, label: str):
        stale = [k for k, v in store.items() if (now - v.get("last_seen", 0)) > timeout_s]
        for k in stale:
            LOG.debug("Pruning stale %s track: %s", label, k)
            del store[k]

    # ------------------------------------------------------------------
    # GPS fix
    # ------------------------------------------------------------------
    def update_gps_fix(self, lat: float, lon: float, *,
                        altitude_m: Optional[float] = None,
                        heading: Optional[float] = None,
                        speed_mps: Optional[float] = None,
                        fix_quality: Optional[str] = None):
        with self._lock:
            self._gps_fix = {
                "lat": lat,
                "lon": lon,
                "altitude_m": altitude_m,
                "heading": heading,
                "speed_mps": speed_mps,
                "fix_quality": fix_quality,
                "last_seen": _monotonic(),
                "last_seen_iso": _iso_now(),
            }

    def get_gps_fix(self) -> Optional[dict]:
        with self._lock:
            if self._gps_fix is None:
                return None
            fix = dict(self._gps_fix)
        fix["age_s"] = _monotonic() - fix["last_seen"]
        fix["stale"] = fix["age_s"] > self._timeouts["gps"]
        return fix

    # ------------------------------------------------------------------
    # ADS-B
    # ------------------------------------------------------------------
    def update_adsb_tracks(self, tracks: List[dict]):
        """
        Upsert a batch of ADS-B tracks. Each dict must contain 'icao'.
        Tracks NOT in this batch are left alone (they age out naturally
        via the prune thread) — this matches tar1090 polling semantics
        where a momentary poll glitch shouldn't drop a track.
        """
        now = _monotonic()
        with self._lock:
            for track in tracks:
                icao = track.get("icao") or track.get("hex")
                if not icao:
                    LOG.debug("Skipping ADS-B track with no icao/hex identifier: %r", track)
                    continue
                entry = dict(track)
                entry["last_seen"] = now
                self._adsb[icao] = entry

    # ------------------------------------------------------------------
    # Remote ID (WiFi + Bluetooth), merged by basic_id
    # ------------------------------------------------------------------
    def update_remoteid(self, source: str, track: dict):
        """
        Upsert a single Remote ID observation.

        source: "wifi" or "bluetooth"
        track: must contain 'track_key' (stable per-session id, e.g. MAC).
               May contain 'basic_id' once decoded — this is what enables
               cross-source merging with the other capture module.
        """
        if source not in ("wifi", "bluetooth"):
            raise ValueError(f"Unknown remote id source: {source!r}")

        track_key = track.get("track_key")
        if not track_key:
            LOG.debug("Skipping remote ID track with no track_key: %r", track)
            return

        store = self._remoteid_wifi if source == "wifi" else self._remoteid_bt
        with self._lock:
            entry = dict(track)
            entry["last_seen"] = _monotonic()
            store[track_key] = entry

    # ------------------------------------------------------------------
    # Zones
    # ------------------------------------------------------------------
    def update_zones(self, zones: List[dict]):
        """Replace the full zone set (called by airspace_manager after
        each refresh cycle)."""
        with self._lock:
            self._zones = [dict(z) for z in zones]

    def get_zones(self) -> List[dict]:
        with self._lock:
            return [dict(z) for z in self._zones]

    # ------------------------------------------------------------------
    # Proximity alerts
    # ------------------------------------------------------------------
    def update_proximity_alerts(self, alerts: List[dict]):
        with self._lock:
            self._alerts = [dict(a) for a in alerts]

    def get_proximity_alerts(self) -> List[dict]:
        with self._lock:
            return [dict(a) for a in self._alerts]

    # ------------------------------------------------------------------
    # Snapshot for GUI rendering
    # ------------------------------------------------------------------
    def get_snapshot(self) -> Dict[str, Any]:
        """
        Return a fully merged, position-filtered, icon-resolved view of
        the world for the GUI's render loop. Tracks without a resolvable
        lat/lon are excluded entirely (per design decision).
        """
        with self._lock:
            adsb_items = list(self._adsb.items())
            wifi_items = dict(self._remoteid_wifi)
            bt_items = dict(self._remoteid_bt)
            zones = [dict(z) for z in self._zones]
            alerts = [dict(a) for a in self._alerts]
            gps_fix = dict(self._gps_fix) if self._gps_fix else None

        tracks: List[dict] = []

        # --- ADS-B tracks ---
        for icao, entry in adsb_items:
            lat, lon = entry.get("lat"), entry.get("lon")
            if lat is None or lon is None:
                continue
            tracks.append({
                "id": f"adsb:{icao}",
                "source": "adsb",
                "lat": lat,
                "lon": lon,
                "altitude_m": entry.get("altitude_m"),
                "heading": entry.get("heading"),
                "speed_mps": entry.get("speed_mps"),
                "callsign": entry.get("callsign"),
                "basic_id": None,
                "icon": ICON_PLANE,
            })

        # --- Remote ID: merge by basic_id where possible ---
        merged_basic_ids = set()
        for entry in list(wifi_items.values()) + list(bt_items.values()):
            bid = entry.get("basic_id")
            if bid:
                merged_basic_ids.add(bid)

        for bid in merged_basic_ids:
            wifi_entry = next((e for e in wifi_items.values() if e.get("basic_id") == bid), None)
            bt_entry = next((e for e in bt_items.values() if e.get("basic_id") == bid), None)

            candidates = [e for e in (wifi_entry, bt_entry) if e is not None]
            primary = max(candidates, key=lambda e: e.get("last_seen", 0))

            lat, lon = primary.get("lat"), primary.get("lon")
            if lat is None or lon is None:
                continue  # neither source has a position fix yet for this basic_id

            source_count = sum(1 for e in (wifi_entry, bt_entry) if e is not None)
            icon = ICON_DRONE_CROSS_VERIFIED if source_count >= 2 else ICON_DRONE_SINGLE_SOURCE

            tracks.append({
                "id": f"remoteid:{bid}",
                "source": "remoteid_merged" if source_count >= 2 else (
                    "remoteid_wifi" if wifi_entry is primary else "remoteid_bt"
                ),
                "lat": lat,
                "lon": lon,
                "altitude_m": primary.get("altitude_m"),
                "heading": primary.get("heading"),
                "speed_mps": primary.get("speed_mps"),
                "callsign": primary.get("callsign"),
                "basic_id": bid,
                "icon": icon,
            })

        # --- Remote ID: entries with no decoded basic_id (unmerged, unknown identity) ---
        for source_label, store in (("remoteid_wifi", wifi_items), ("remoteid_bt", bt_items)):
            for track_key, entry in store.items():
                if entry.get("basic_id"):
                    continue  # already handled above via basic_id merge
                lat, lon = entry.get("lat"), entry.get("lon")
                if lat is None or lon is None:
                    continue
                tracks.append({
                    "id": f"{source_label}:{track_key}",
                    "source": source_label,
                    "lat": lat,
                    "lon": lon,
                    "altitude_m": entry.get("altitude_m"),
                    "heading": entry.get("heading"),
                    "speed_mps": entry.get("speed_mps"),
                    "callsign": None,
                    "basic_id": None,
                    "icon": ICON_UNKNOWN,
                })

        return {
            "gps": self._decorate_gps(gps_fix),
            "tracks": tracks,
            "zones": zones,
            "alerts": alerts,
            "counts": {
                "adsb": len(adsb_items),
                "remoteid_wifi": len(wifi_items),
                "remoteid_bt": len(bt_items),
                "tracks_rendered": len(tracks),
                "zones": len(zones),
                "alerts": len(alerts),
            },
            "timestamp": _iso_now(),
        }

    def _decorate_gps(self, gps_fix: Optional[dict]) -> Optional[dict]:
        if gps_fix is None:
            return None
        age_s = _monotonic() - gps_fix["last_seen"]
        gps_fix["age_s"] = age_s
        gps_fix["stale"] = age_s > self._timeouts["gps"]
        return gps_fix


# ----------------------------------------------------------------------
# Standalone smoke test
# ----------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    f = Fusion(timeouts={"adsb": 2, "remoteid": 2, "gps": 2}, prune_interval_s=1)
    f.start()

    f.update_gps_fix(51.5, -0.1)
    f.update_adsb_tracks([{"icao": "4CA123", "lat": 51.51, "lon": -0.09, "callsign": "BAW123"}])
    f.update_remoteid("wifi", {"track_key": "AA:BB:CC:DD:EE:FF", "basic_id": "DRONE123",
                                "lat": 51.505, "lon": -0.11})
    f.update_remoteid("bluetooth", {"track_key": "FF:11:22", "basic_id": "DRONE123",
                                     "lat": 51.5051, "lon": -0.1101})
    f.update_remoteid("wifi", {"track_key": "11:22:33", "lat": 51.52, "lon": -0.12})  # no basic_id

    snap = f.get_snapshot()
    print("Tracks:")
    for t in snap["tracks"]:
        print(f"  {t['id']:<30} icon={t['icon']} source={t['source']}")

    print("Waiting for prune...")
    time.sleep(3)
    snap = f.get_snapshot()
    print("Tracks after prune:", len(snap["tracks"]))
    f.stop()
