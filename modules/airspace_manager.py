"""
modules/airspace_manager.py — Airspace / NOTAM / FRZ / runway data authority

Responsibilities:
    - Load static geofence zones from data/geofences/uk_zones.geojson
    - Regenerate FRZ approximations *only when source files change on
      disk* (mtime comparison against a persisted state file) — NOT on
      a timer, per project decision.
    - Import manually-supplied NOTAM/zone files from a watch directory
    - Optionally sync remote sources via pluggable modules
      (modules/openaip_sync.py, modules/notam_import.py). These are
      OPTIONAL imports — if not present yet, this manager logs a single
      warning and continues without them (keeps the app runnable while
      those modules are still being written).
    - Prune expired zones (based on `expiry` field, ISO-8601)
    - Load runway heading data with confidence indicators, excluding
      non-active airfields (e.g. Digby)
    - Maintain a thread-safe merged zone set and push it into fusion.py
      via fusion.update_zones()
    - Run a background refresh loop with configurable interval and
      exponential backoff on error; support immediate manual retry
      (used by the GUI's freshness badge / settings "Retry" button)
    - Expose get_status() in the shape expected by gui/app.py's
      FreshnessBadge: {"state": "ok"|"stale"|"error"|"unknown",
                       "last_sync": iso_str_or_None, "error": str_or_None}

Zone dict shape produced by this module (consumed by fusion.py + GUI):
    {
        "id": str,                      # unique, e.g. "frz:heathrow"
        "name": str,
        "type": str,                    # "FRZ" | "NOTAM" | "Restricted" | ...
        "polygon": [(lat, lon), ...],   # exterior ring, closed or open
        "verified": bool,
        "expiry": str | None,           # ISO-8601, None = no expiry
        "source": str,                  # e.g. "static", "frz_generated",
                                         # "manual_import:<file>", "openaip"
    }

Runway dict shape:
    {
        "airfield": str,
        "runway": str,          # e.g. "09/27"
        "heading_true": float,
        "confidence": "high" | "medium" | "low",
        "verified": bool,
    }
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

LOG = logging.getLogger("modules.airspace_manager")

# ----------------------------------------------------------------------
# Optional pluggable sync modules — not required for this manager to run
# ----------------------------------------------------------------------
try:
    from modules import openaip_sync  # type: ignore
except ImportError:
    openaip_sync = None  # noqa: N816

try:
    from modules import notam_import  # type: ignore
except ImportError:
    notam_import = None  # noqa: N816

# ----------------------------------------------------------------------
# Constants / defaults
# ----------------------------------------------------------------------
RUNWAY_EXCLUDE = {"Digby"}  # non-active airfields excluded from display

DEFAULT_CONFIG: Dict[str, Any] = {
    "geofence_path": "data/geofences/uk_zones.geojson",
    "frz_source_dir": "data/notam/frz_sources",
    "frz_state_path": "data/state/frz_state.json",
    "manual_notam_dir": "data/notam/manual",
    "runway_data_path": "data/airfields/runways.json",
    "refresh_interval_s": 3600,       # 1 hour baseline
    "backoff_max_s": 21600,           # 6 hours max backoff
    "stale_after_multiple": 2,        # stale if last_sync older than N x interval
    "remote_sync": {
        "openaip_enabled": False,
        "notam_enabled": False,
    },
}

EARTH_RADIUS_M = 6371000


# ----------------------------------------------------------------------
# Geometry helpers
# ----------------------------------------------------------------------
def _destination_point(lat: float, lon: float, distance_m: float, bearing_deg: float):
    """Compute a destination lat/lon given a start point, distance, and bearing."""
    ang_dist = distance_m / EARTH_RADIUS_M
    bearing = math.radians(bearing_deg)
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)

    lat2 = math.asin(
        math.sin(lat1) * math.cos(ang_dist)
        + math.cos(lat1) * math.sin(ang_dist) * math.cos(bearing)
    )
    lon2 = lon1 + math.atan2(
        math.sin(bearing) * math.sin(ang_dist) * math.cos(lat1),
        math.cos(ang_dist) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lon2)


def _circle_polygon(lat: float, lon: float, radius_m: float, num_points: int = 32):
    """
    Generate an approximate circular polygon around a point — used for
    FRZ generation, since UK drone Flight Restriction Zones are
    typically defined as a radius around an airport's ARP but the true
    boundary may differ slightly. Hence these are always marked
    verified=False ("⚠️ Approximate FRZ").
    """
    points = []
    for i in range(num_points):
        bearing = (360.0 / num_points) * i
        points.append(_destination_point(lat, lon, radius_m, bearing))
    return points


def _polygon_from_geojson_coords(coords) -> Optional[List[tuple]]:
    """
    Convert GeoJSON Polygon 'coordinates' (exterior ring only, ignoring
    holes) — which are [lon, lat] pairs — into our internal (lat, lon)
    tuple list.
    """
    if not coords:
        return None
    exterior = coords[0]  # ignore interior rings/holes
    try:
        return [(pt[1], pt[0]) for pt in exterior]
    except (IndexError, TypeError):
        return None


# ----------------------------------------------------------------------
# Time helpers
# ----------------------------------------------------------------------
def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        LOG.warning("Unparsable ISO timestamp %r — treating as no-expiry", value)
        return None


# ----------------------------------------------------------------------
# AirspaceManager
# ----------------------------------------------------------------------
class AirspaceManager:
    def __init__(self, config: Optional[Dict[str, Any]] = None, fusion=None):
        cfg = dict(DEFAULT_CONFIG)
        if config:
            cfg.update({k: v for k, v in config.items() if k != "remote_sync"})
            if "remote_sync" in config:
                merged_remote = dict(DEFAULT_CONFIG["remote_sync"])
                merged_remote.update(config["remote_sync"])
                cfg["remote_sync"] = merged_remote
        self.config = cfg
        self.fusion = fusion

        self._lock = threading.RLock()
        self._zones: Dict[str, dict] = {}
        self._runways: List[dict] = []
        self._status: Dict[str, Any] = {"state": "unknown", "last_sync": None, "error": None}

        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._base_interval = float(cfg["refresh_interval_s"])
        self._backoff_max = float(cfg["backoff_max_s"])
        self._current_interval = self._base_interval

        self._frz_state_path = Path(cfg["frz_state_path"])
        self._warned_missing_openaip = False
        self._warned_missing_notam = False

        # Runways are fairly static reference data — load once eagerly
        # so get_runways() has data even before the first refresh cycle.
        self._load_runways()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            LOG.warning("AirspaceManager.start() called but thread already running")
            return
        LOG.info("Starting AirspaceManager background refresh thread")
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="airspace-manager", daemon=True
        )
        self._thread.start()

    def stop(self):
        LOG.info("Stopping AirspaceManager background refresh thread")
        self._stop_event.set()
        self._wake_event.set()
        if self._thread:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                LOG.warning("AirspaceManager thread did not stop within timeout")

    def manual_retry(self):
        """
        Called by the GUI's Settings "Retry Airspace Sync Now" button
        and by the FreshnessBadge's inline retry action. Resets backoff
        and wakes the refresh loop immediately.
        """
        LOG.info("Manual airspace refresh requested")
        self._current_interval = self._base_interval
        self._wake_event.set()

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------
    def _run_loop(self):
        while not self._stop_event.is_set():
            try:
                self._do_refresh_cycle()
                with self._lock:
                    self._status = {"state": "ok", "last_sync": _iso_now(), "error": None}
                self._current_interval = self._base_interval
                LOG.info("Airspace refresh cycle completed OK (%d zones)", len(self._zones))
            except Exception as exc:
                LOG.exception("Airspace refresh cycle failed")
                with self._lock:
                    self._status["state"] = "error"
                    self._status["error"] = str(exc)
                self._current_interval = min(self._current_interval * 2, self._backoff_max)
                LOG.warning("Backing off next airspace refresh to %.0fs", self._current_interval)

            self._wake_event.wait(timeout=self._current_interval)
            self._wake_event.clear()

    def _do_refresh_cycle(self):
        with self._lock:
            self._load_static_geofences()
            self._maybe_regenerate_frz()
            self._import_manual_notams()
            self._sync_remote_sources()
            self._prune_expired_zones()
            self._push_to_fusion()

    # ------------------------------------------------------------------
    # Public read accessors (thread-safe)
    # ------------------------------------------------------------------
    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            status = dict(self._status)

        last_sync = _parse_iso(status.get("last_sync"))
        if status["state"] != "error" and last_sync is not None:
            age_s = (datetime.now(timezone.utc) - last_sync).total_seconds()
            stale_threshold = self._base_interval * self.config["stale_after_multiple"]
            if age_s > stale_threshold:
                status["state"] = "stale"
        return status

    def get_zones(self) -> List[dict]:
        with self._lock:
            return [dict(z) for z in self._zones.values()]

    def get_runways(self) -> List[dict]:
        with self._lock:
            return [
                dict(rw) for rw in self._runways
                if rw.get("airfield") not in RUNWAY_EXCLUDE
            ]

    # ------------------------------------------------------------------
    # Static geofence loading (uk_zones.geojson)
    # ------------------------------------------------------------------
    def _load_static_geofences(self):
        path = Path(self.config["geofence_path"])
        if not path.exists():
            LOG.warning("Static geofence file not found: %s", path)
            return

        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Failed to parse geofence file {path}: {exc}") from exc

        count = 0
        for feature in data.get("features", []):
            geom = feature.get("geometry", {})
            props = feature.get("properties", {}) or {}

            if geom.get("type") != "Polygon":
                continue  # MultiPolygon / other types not yet supported
            polygon = _polygon_from_geojson_coords(geom.get("coordinates"))
            if not polygon:
                continue

            name = props.get("name", "Unnamed Zone")
            zone_id = f"static:{name}"
            self._zones[zone_id] = {
                "id": zone_id,
                "name": name,
                "type": props.get("type", "Zone"),
                "polygon": polygon,
                "verified": bool(props.get("verified", True)),
                "expiry": props.get("expiry"),
                "source": "static",
            }
            count += 1

        LOG.debug("Loaded %d static geofence zones from %s", count, path)

    # ------------------------------------------------------------------
    # FRZ regeneration — triggered ONLY on source file mtime change
    # ------------------------------------------------------------------
    def _maybe_regenerate_frz(self):
        source_dir = Path(self.config["frz_source_dir"])
        if not source_dir.exists():
            LOG.debug("FRZ source dir %s does not exist — skipping FRZ generation", source_dir)
            return

        source_files = sorted(source_dir.glob("*.json"))
        if not source_files:
            return

        latest_mtime = max(f.stat().st_mtime for f in source_files)
        last_known_mtime = self._read_frz_state_mtime()

        if last_known_mtime is not None and latest_mtime <= last_known_mtime:
            LOG.debug("FRZ sources unchanged since last generation — skipping regeneration")
            return

        LOG.info("FRZ source data changed on disk — regenerating FRZ zones")
        generated = self._generate_frz_zones(source_files)

        # Remove previously-generated FRZ zones before inserting the new set
        stale_ids = [zid for zid, z in self._zones.items() if z["source"] == "frz_generated"]
        for zid in stale_ids:
            del self._zones[zid]

        for zone in generated:
            self._zones[zone["id"]] = zone

        self._write_frz_state_mtime(latest_mtime)
        LOG.info("FRZ regeneration complete: %d zone(s) generated", len(generated))

    def _generate_frz_zones(self, source_files: List[Path]) -> List[dict]:
        """
        Each source file describes a Flight Restriction Zone origin
        point (e.g. an airport ARP) and radius, per UK drone-code style
        FRZs. Expected file shape:

            {
                "name": "Heathrow FRZ",
                "lat": 51.4700,
                "lon": -0.4543,
                "radius_m": 5000,
                "type": "FRZ",
                "expiry": null
            }

        Generated polygons are always marked verified=False, since a
        circular approximation of a real (often irregular) FRZ boundary
        is inherently approximate — this is what surfaces as the
        "⚠️ Approximate FRZ — verified: false" label in the GUI.
        """
        zones = []
        for file in source_files:
            try:
                spec = json.loads(file.read_text())
            except (OSError, json.JSONDecodeError):
                LOG.exception("Failed to parse FRZ source file %s — skipping", file)
                continue

            required = {"name", "lat", "lon", "radius_m"}
            if not required.issubset(spec):
                LOG.warning("FRZ source file %s missing required fields %s — skipping",
                            file, required - set(spec))
                continue

            polygon = _circle_polygon(spec["lat"], spec["lon"], spec["radius_m"])
            zone_id = f"frz_generated:{spec['name']}"
            zones.append({
                "id": zone_id,
                "name": spec["name"],
                "type": spec.get("type", "FRZ"),
                "polygon": polygon,
                "verified": False,
                "expiry": spec.get("expiry"),
                "source": "frz_generated",
            })
        return zones

    def _read_frz_state_mtime(self) -> Optional[float]:
        if not self._frz_state_path.exists():
            return None
        try:
            state = json.loads(self._frz_state_path.read_text())
            return state.get("last_source_mtime")
        except (OSError, json.JSONDecodeError):
            LOG.warning("Could not read FRZ state file %s — assuming no prior state",
                        self._frz_state_path)
            return None

    def _write_frz_state_mtime(self, mtime: float):
        self._frz_state_path.parent.mkdir(parents=True, exist_ok=True)
        self._frz_state_path.write_text(json.dumps({"last_source_mtime": mtime}))

    def force_regenerate_frz(self):
        """External hook (e.g. a future 'force regenerate' button) that
        bypasses the mtime check entirely."""
        with self._lock:
            if self._frz_state_path.exists():
                self._frz_state_path.unlink()
            self._maybe_regenerate_frz()
            self._push_to_fusion()

    # ------------------------------------------------------------------
    # Manual NOTAM / zone import
    # ------------------------------------------------------------------
    def _import_manual_notams(self):
        manual_dir = Path(self.config["manual_notam_dir"])
        if not manual_dir.exists():
            return

        for file in sorted(manual_dir.glob("*.json")):
            try:
                self._import_manual_notam_file(file)
            except Exception:
                LOG.exception("Failed to import manual NOTAM file %s — skipping", file)

    def import_manual_notam_file(self, path: Path):
        """
        Public entry point — e.g. for a future GUI "Import NOTAM" file
        picker — that imports a single file and immediately pushes the
        updated zone set to fusion.
        """
        with self._lock:
            self._import_manual_notam_file(Path(path))
            self._push_to_fusion()

    def _import_manual_notam_file(self, file: Path):
        """
        Expected manual NOTAM file shape (single zone or list of zones):

            {
                "name": "Temp Restricted Area - Airshow",
                "type": "NOTAM",
                "polygon": [[lat, lon], [lat, lon], ...],
                "verified": true,
                "expiry": "2025-07-01T00:00:00+00:00"
            }

        Manually imported zones default to verified=True (assumption:
        a human has reviewed the source before placing the file here);
        this can be overridden explicitly in the file itself.
        """
        raw = json.loads(file.read_text())
        entries = raw if isinstance(raw, list) else [raw]

        for entry in entries:
            polygon = entry.get("polygon")
            if not polygon or len(polygon) < 3:
                LOG.warning("Manual NOTAM entry in %s missing valid polygon — skipping", file)
                continue

            name = entry.get("name", file.stem)
            zone_id = f"manual_import:{file.name}:{name}"
            self._zones[zone_id] = {
                "id": zone_id,
                "name": name,
                "type": entry.get("type", "NOTAM"),
                "polygon": [tuple(pt) for pt in polygon],
                "verified": bool(entry.get("verified", True)),
                "expiry": entry.get("expiry"),
                "source": f"manual_import:{file.name}",
            }

    # ------------------------------------------------------------------
    # Optional remote sync (pluggable, gracefully degraded)
    # ------------------------------------------------------------------
    def _sync_remote_sources(self):
        remote_cfg = self.config.get("remote_sync", {})

        if remote_cfg.get("openaip_enabled"):
            if openaip_sync is None:
                if not self._warned_missing_openaip:
                    LOG.warning(
                        "airspace.remote_sync.openaip_enabled is true but "
                        "modules/openaip_sync.py is not present — skipping "
                        "(this warning will not repeat)"
                    )
                    self._warned_missing_openaip = True
            else:
                try:
                    fetched = openaip_sync.fetch_zones(self.config)  # type: ignore[attr-defined]
                    for zone in fetched:
                        self._zones[zone["id"]] = zone
                except Exception:
                    LOG.exception("openaip_sync.fetch_zones() failed")
                    raise  # bubble up so this refresh cycle is marked as error/backoff

        if remote_cfg.get("notam_enabled"):
            if notam_import is None:
                if not self._warned_missing_notam:
                    LOG.warning(
                        "airspace.remote_sync.notam_enabled is true but "
                        "modules/notam_import.py is not present — skipping "
                        "(this warning will not repeat)"
                    )
                    self._warned_missing_notam = True
            else:
                try:
                    fetched = notam_import.fetch_notams(self.config)  # type: ignore[attr-defined]
                    for zone in fetched:
                        self._zones[zone["id"]] = zone
                except Exception:
                    LOG.exception("notam_import.fetch_notams() failed")
                    raise

    # ------------------------------------------------------------------
    # Expiry pruning
    # ------------------------------------------------------------------
    def _prune_expired_zones(self):
        now = datetime.now(timezone.utc)
        expired_ids = []
        for zid, zone in self._zones.items():
            expiry = _parse_iso(zone.get("expiry"))
            if expiry is not None and expiry < now:
                expired_ids.append(zid)

        for zid in expired_ids:
            LOG.info("Pruning expired zone: %s (expired %s)",
                      self._zones[zid]["name"], self._zones[zid]["expiry"])
            del self._zones[zid]

    # ------------------------------------------------------------------
    # Runway data (heading, confidence, verification)
    # ------------------------------------------------------------------
    def _load_runways(self):
        path = Path(self.config["runway_data_path"])
        if not path.exists():
            LOG.warning("Runway data file not found: %s", path)
            return

        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            LOG.exception("Failed to parse runway data file %s", path)
            return

        runways = []
        for entry in data.get("runways", data if isinstance(data, list) else []):
            runways.append({
                "airfield": entry.get("airfield", "Unknown"),
                "runway": entry.get("runway", "?"),
                "heading_true": entry.get("heading_true"),
                "confidence": entry.get("confidence", "low"),
                "verified": bool(entry.get("verified", False)),
            })

        with self._lock:
            self._runways = runways
        LOG.info("Loaded %d runway record(s) (excluding %s from display)",
                  len(runways), ", ".join(RUNWAY_EXCLUDE))

    def reload_runways(self):
        """External hook for a future 'reload runway data' action."""
        self._load_runways()

    # ------------------------------------------------------------------
    # Fusion integration
    # ------------------------------------------------------------------
    def _push_to_fusion(self):
        if self.fusion is None:
            return
        try:
            self.fusion.update_zones(list(self._zones.values()))
        except AttributeError:
            LOG.warning(
                "fusion object has no update_zones() method — zones not pushed. "
                "Confirm fusion.py exposes update_zones(zones: list)."
            )


# ----------------------------------------------------------------------
# Standalone smoke test
# ----------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    mgr = AirspaceManager()
    mgr._do_refresh_cycle()  # synchronous one-shot for manual testing
    print("Status:", mgr.get_status())
    print("Zones:", len(mgr.get_zones()))
    for z in mgr.get_zones():
        flag = "✅" if z["verified"] else "⚠️ verified: false"
        print(f"  - {z['name']} [{z['type']}] {flag} (source={z['source']})")
    print("Runways:", len(mgr.get_runways()))
