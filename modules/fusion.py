"""
modules/fusion.py — Shared state hub for tracks, GPS fix, zones, alerts

Thread-safety model:
  - Single `threading.RLock` guards all mutable state
  - Writers (GPS/WiFi/BT/ADS-B/AirspaceManager/ProximityAlertMonitor)
    call update_*() methods — fast, lock-held-briefly
  - Readers (GUI, ProximityAlertMonitor) call get_*() / get_snapshot()
    which return *copies*, never live references, so the GUI can
    iterate without holding the lock

Track identity & merging:
  - ADS-B tracks are keyed by ICAO hex (unique per aircraft)
  - Remote ID tracks are keyed by `basic_id` (UAS ID from ASTM F3411)
  - If the *same* basic_id is seen via both WiFi and Bluetooth, the
    tracks are merged into one, with `sources` recording both — this
    avoids showing the same drone twice on the map

Staleness / pruning:
  - Each track records `last_seen` (monotonic time)
  - A background daemon thread prunes tracks older than their
    source's configured timeout, once per `prune_interval_s`
"""

from __future__ import annotations

import time
import logging
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

LOG = logging.getLogger("fusion")


# ---------------------------------------------------------------------------
# Enums / constants
# ---------------------------------------------------------------------------
class TrackSource(str, Enum):
    ADSB = "adsb"
    WIFI = "wifi"
    BLUETOOTH = "bluetooth"


class TrackType(str, Enum):
    AIRCRAFT = "aircraft"
    DRONE_VERIFIED = "drone_verified"      # Remote ID with location + basic_id
    DRONE_PARTIAL = "drone_partial"        # Remote ID seen, location/type incomplete
    UNKNOWN = "unknown"


ICON_MAP = {
    TrackType.AIRCRAFT: "images/icons/plane_blue.png",
    TrackType.DRONE_VERIFIED: "images/icons/drone_orange.png",
    TrackType.DRONE_PARTIAL: "images/icons/drone_purple.png",
    TrackType.UNKNOWN: "images/icons/unknown_grey.png",
}
OWNSHIP_ICON = "images/icons/ownship.png"


# ---------------------------------------------------------------------------
# Track dataclass
# ---------------------------------------------------------------------------
@dataclass
class Track:
    track_id: str                       # ICAO hex or Remote ID basic_id
    sources: set[TrackSource] = field(default_factory=set)
    track_type: TrackType = TrackType.UNKNOWN

    lat: Optional[float] = None
    lon: Optional[float] = None
    alt_m: Optional[float] = None
    heading_deg: Optional[float] = None
    speed_mps: Optional[float] = None

    callsign: Optional[str] = None      # ADS-B callsign
    basic_id: Optional[str] = None      # Remote ID UAS ID
    ua_type: Optional[str] = None       # Remote ID UA type string, if known

    last_seen: float = field(default_factory=time.monotonic)
    first_seen: float = field(default_factory=time.monotonic)
    raw: dict[str, Any] = field(default_factory=dict)  # last raw payload, for debugging

    def age_s(self) -> float:
        return time.monotonic() - self.last_seen

    def has_position(self) -> bool:
        return self.lat is not None and self.lon is not None


# ---------------------------------------------------------------------------
# FusionEngine
# ---------------------------------------------------------------------------
class FusionEngine:
    """Central thread-safe store for GPS fix, tracks, zones, and alerts."""

    def __init__(self, config: dict):
        self.config = config
        self._lock = threading.RLock()

        # --- GPS ---
        self._gps_fix: Optional[dict] = None
        self._gps_fix_timeout_s = config.get("gps", {}).get("fix_timeout_s", 10)

        # --- Tracks ---
        self._tracks: dict[str, Track] = {}
        self._adsb_stale_after_s = config.get("adsb", {}).get("stale_after_s", 30)
        self._remoteid_stale_after_s = config.get("remoteid", {}).get("stale_after_s", 20)

        # --- Zones (geofences / FRZ / NOTAM-derived) ---
        self._zones: list[dict] = []          # list of GeoJSON-like features
        self._zones_updated_at: Optional[float] = None

        # --- Proximity alerts ---
        self._proximity_alerts: list[dict] = []
        self._proximity_updated_at: Optional[float] = None

        # --- Background pruning ---
        self._prune_interval_s = config.get("fusion", {}).get("prune_interval_s", 5)
        self._stop_event = threading.Event()
        self._prune_thread: Optional[threading.Thread] = None

    # =======================================================================
    # Lifecycle
    # =======================================================================
    def start(self):
        """Start the background pruning thread. Call once from main.py."""
        if self._prune_thread and self._prune_thread.is_alive():
            return
        self._stop_event.clear()
        self._prune_thread = threading.Thread(
            target=self._prune_loop, name="fusion_prune", daemon=True
        )
        self._prune_thread.start()
        LOG.info("FusionEngine pruning thread started (interval=%ss)",
                 self._prune_interval_s)

    def stop(self):
        self._stop_event.set()
        if self._prune_thread:
            self._prune_thread.join(timeout=3)

    def _prune_loop(self):
        while not self._stop_event.is_set():
            try:
                self._prune_stale_tracks()
            except Exception:
                LOG.exception("Error during track pruning")
            self._stop_event.wait(self._prune_interval_s)

    # =======================================================================
    # GPS fix
    # =======================================================================
    def update_gps_fix(self, fix: dict):
        """
        Called by GPSReader. Expected shape:
          {"lat": float, "lon": float, "alt_m": float,
           "speed_mps": float, "track_deg": float, "mode": int, "time": str}
        """
        with self._lock:
            fix = dict(fix)
            fix["_received_at"] = time.monotonic()
            self._gps_fix = fix

    def get_gps_fix(self) -> Optional[dict]:
        """Returns a copy of the latest GPS fix, or None if stale/unset."""
        with self._lock:
            if self._gps_fix is None:
                return None
            age = time.monotonic() - self._gps_fix["_received_at"]
            if age > self._gps_fix_timeout_s:
                return None
            return dict(self._gps_fix)

    def has_valid_fix(self) -> bool:
        return self.get_gps_fix() is not None

    # =======================================================================
    # ADS-B tracks (bulk update each poll)
    # =======================================================================
    def update_adsb_tracks(self, aircraft_list: list[dict]):
        """
        Called by ADSBIngest with the parsed contents of aircraft.json's
        "aircraft" list. Each entry expected to have at least "hex".
        """
        now = time.monotonic()
        with self._lock:
            for ac in aircraft_list:
                hex_id = ac.get("hex")
                if not hex_id:
                    continue

                track = self._tracks.get(hex_id)
                if track is None:
                    track = Track(track_id=hex_id, track_type=TrackType.AIRCRAFT)
                    self._tracks[hex_id] = track

                track.sources.add(TrackSource.ADSB)
                track.track_type = TrackType.AIRCRAFT
                track.lat = ac.get("lat", track.lat)
                track.lon = ac.get("lon", track.lon)
                track.alt_m = _feet_to_m(ac.get("alt_baro")) if ac.get("alt_baro") else track.alt_m
                track.heading_deg = ac.get("track", track.heading_deg)
                track.speed_mps = _knots_to_mps(ac.get("gs")) if ac.get("gs") else track.speed_mps
                track.callsign = ac.get("flight", track.callsign)
                track.last_seen = now
                track.raw = ac

    # =======================================================================
    # Remote ID tracks (WiFi + Bluetooth, single-detection updates)
    # =======================================================================
    def update_remoteid_track(self, detection: dict):
        """
        Called by WiFiCapture / BluetoothCapture on each parsed Remote ID
        message. Expected shape (fields optional beyond basic_id):
          {
            "source": "wifi" | "bluetooth",
            "basic_id": str,
            "lat": float, "lon": float,
            "alt_m": float, "heading_deg": float, "speed_mps": float,
            "ua_type": str,
          }
        """
        basic_id = detection.get("basic_id")
        if not basic_id:
            LOG.debug("Remote ID detection missing basic_id — dropping: %s", detection)
            return

        source_str = detection.get("source", "wifi")
        try:
            source = TrackSource(source_str)
        except ValueError:
            source = TrackSource.WIFI

        now = time.monotonic()
        with self._lock:
            track = self._tracks.get(basic_id)
            if track is None:
                track = Track(track_id=basic_id, basic_id=basic_id)
                self._tracks[basic_id] = track

            track.sources.add(source)
            track.ua_type = detection.get("ua_type", track.ua_type)

            if detection.get("lat") is not None and detection.get("lon") is not None:
                track.lat = detection["lat"]
                track.lon = detection["lon"]

            track.alt_m = detection.get("alt_m", track.alt_m)
            track.heading_deg = detection.get("heading_deg", track.heading_deg)
            track.speed_mps = detection.get("speed_mps", track.speed_mps)
            track.last_seen = now
            track.raw = detection

            # Classify drone confidence based on data completeness
            if track.has_position() and track.basic_id:
                track.track_type = TrackType.DRONE_VERIFIED
            else:
                track.track_type = TrackType.DRONE_PARTIAL

    # =======================================================================
    # Pruning
    # =======================================================================
    def _prune_stale_tracks(self):
        with self._lock:
            to_remove = []
            for track_id, track in self._tracks.items():
                timeout = (
                    self._adsb_stale_after_s
                    if TrackSource.ADSB in track.sources
                    else self._remoteid_stale_after_s
                )
                if track.age_s() > timeout:
                    to_remove.append(track_id)

            for track_id in to_remove:
                LOG.debug("Pruning stale track %s (age=%.1fs)",
                          track_id, self._tracks[track_id].age_s())
                del self._tracks[track_id]

            if to_remove:
                LOG.info("Pruned %d stale track(s)", len(to_remove))

    # =======================================================================
    # Geofence / FRZ zones
    # =======================================================================
    def update_geofence_zones(self, zones: list[dict]):
        """
        Called by AirspaceManager whenever zones are (re)loaded — e.g.
        after FRZ regeneration or NOTAM import. Wholesale replace, since
        zone identity/versioning is AirspaceManager's job, not fusion's.

        Each zone expected to be a GeoJSON Feature-like dict, at minimum:
          {"geometry": {...}, "properties": {"name": str, "verified": bool, ...}}
        """
        with self._lock:
            self._zones = zones
            self._zones_updated_at = time.monotonic()
        LOG.info("Geofence zones updated: %d zone(s)", len(zones))

    def get_zones(self) -> list[dict]:
        with self._lock:
            return list(self._zones)

    def zones_age_s(self) -> Optional[float]:
        with self._lock:
            if self._zones_updated_at is None:
                return None
            return time.monotonic() - self._zones_updated_at

    # =======================================================================
    # Proximity alerts
    # =======================================================================
    def update_proximity_alerts(self, alerts: list[dict]):
        """
        Called by ProximityAlertMonitor. Expected shape per alert:
          {"zone_name": str, "distance_m": float, "verified": False, "zone": {...}}
        """
        with self._lock:
            self._proximity_alerts = alerts
            self._proximity_updated_at = time.monotonic()

    def get_proximity_alerts(self) -> list[dict]:
        with self._lock:
            return list(self._proximity_alerts)

    # =======================================================================
    # GUI-facing snapshot
    # =======================================================================
    def get_snapshot(self) -> dict:
        """
        Single call for the GUI's render loop — avoids multiple lock
        acquisitions per frame and pre-resolves icon paths so gui/app.py
        stays dumb about fusion internals.

        Returns:
          {
            "gps_fix": {...} | None,
            "tracks": [
              {"id", "lat", "lon", "heading_deg", "icon", "label",
               "track_type", "sources", "age_s"}, ...
            ],
            "zones": [...],
            "proximity_alerts": [...],
          }
        """
        with self._lock:
            fix = self.get_gps_fix()  # re-enters lock safely (RLock)

            tracks_out = []
            for track in self._tracks.values():
                if not track.has_position():
                    continue  # can't plot without a position
                tracks_out.append({
                    "id": track.track_id,
                    "lat": track.lat,
                    "lon": track.lon,
                    "heading_deg": track.heading_deg,
                    "icon": ICON_MAP.get(track.track_type, ICON_MAP[TrackType.UNKNOWN]),
                    "label": track.callsign or track.basic_id or track.track_id,
                    "track_type": track.track_type.value,
                    "sources": [s.value for s in track.sources],
                    "age_s": round(track.age_s(), 1),
                })

            return {
                "gps_fix": fix,
                "ownship_icon": OWNSHIP_ICON,
                "tracks": tracks_out,
                "zones": list(self._zones),
                "proximity_alerts": list(self._proximity_alerts),
            }

    # =======================================================================
    # Debug / introspection
    # =======================================================================
    def track_count(self) -> int:
        with self._lock:
            return len(self._tracks)

    def get_track(self, track_id: str) -> Optional[Track]:
        with self._lock:
            return self._tracks.get(track_id)


# ---------------------------------------------------------------------------
# Unit helpers (ADS-B feeds use feet / knots)
# ---------------------------------------------------------------------------
def _feet_to_m(feet: float) -> float:
    return feet * 0.3048


def _knots_to_mps(knots: float) -> float:
    return knots * 0.514444
