"""
modules/notam_import.py — Automated NOTAM fetch & parse (pluggable)

Called by modules/airspace_manager.py as:
    notam_import.fetch_notams(config) -> List[zone_dict]

Only invoked when config["remote_sync"]["notam_enabled"] is True.

ASSUMPTIONS (flagged — no confirmed live UK NOTAM API was specified):
    - This module supports TWO input formats from the configured URL:

      (A) "Pre-formatted" feed — a JSON array already matching our
          internal zone schema (id/name/type/polygon/verified/expiry).
          This is the easy path if a future upstream (CAA, NATS,
          openAIP NOTAM bridge, etc.) is adapted to emit this directly.

      (B) "Simple circular NOTAM" feed — a JSON array of raw NOTAM-like
          records with free-text fields, e.g.:
              {
                "id": "A1234/25",
                "text": "AERIAL DISPLAY APRX 2NM RADIUS CENTRE 513026N 0002743W",
                "valid_from": "2025-06-01T00:00:00Z",
                "valid_till": "2025-06-01T18:00:00Z"
              }
          We regex-decode the DMS coordinate + radius out of the free
          text. This is inherently approximate, so these zones are
          ALWAYS marked verified=False (same rationale as generated
          FRZs — surfaces the "⚠️ Approximate" label in the GUI).

    - Format is auto-detected per-record: if a record already has a
      'polygon' key, treated as (A); otherwise (B) is attempted.
    - If neither the network fetch nor decode succeeds, falls back to
      the last successfully cached file on disk (data/cache/notams.json
      by default) so the app keeps running with slightly stale data
      rather than zero NOTAMs.

Config contract (config["notam"], all optional with defaults below):
    {
        "url": None,                       # REQUIRED to actually fetch
        "cache_path": "data/cache/notams.json",
        "timeout_s": 10,
        "default_radius_m": 3704,          # 2NM fallback if radius unparsable
        "default_validity_hours": 24,      # if valid_till missing
    }
"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

LOG = logging.getLogger("modules.notam_import")

try:
    import requests
except ImportError:
    requests = None  # network fetch disabled if requests isn't installed

DEFAULT_CONFIG: Dict[str, Any] = {
    "url": None,
    "cache_path": "data/cache/notams.json",
    "timeout_s": 10,
    "default_radius_m": 3704,          # ~2 NM
    "default_validity_hours": 24,
}

EARTH_RADIUS_M = 6371000

# DMS coordinate pattern, e.g. "513026N 0002743W"
_COORD_RE = re.compile(
    r"(?P<lat_deg>\d{2})(?P<lat_min>\d{2})(?P<lat_sec>\d{2})(?P<lat_hem>[NS])\s*"
    r"(?P<lon_deg>\d{3})(?P<lon_min>\d{2})(?P<lon_sec>\d{2})(?P<lon_hem>[EW])"
)

# Radius pattern, e.g. "2NM RADIUS", "3.5 KM RADIUS", "500M RADIUS"
_RADIUS_RE = re.compile(
    r"(?P<value>\d+(\.\d+)?)\s*(?P<unit>NM|KM|M)\s*RADIUS",
    re.IGNORECASE,
)

_UNIT_TO_METRES = {"NM": 1852.0, "KM": 1000.0, "M": 1.0}


def _dms_to_decimal(deg: str, minute: str, sec: str, hemisphere: str) -> float:
    value = int(deg) + int(minute) / 60.0 + int(sec) / 3600.0
    if hemisphere in ("S", "W"):
        value = -value
    return value


def _circle_polygon(lat: float, lon: float, radius_m: float, num_points: int = 24):
    points = []
    for i in range(num_points):
        bearing = math.radians((360.0 / num_points) * i)
        ang_dist = radius_m / EARTH_RADIUS_M
        lat1, lon1 = math.radians(lat), math.radians(lon)
        lat2 = math.asin(math.sin(lat1) * math.cos(ang_dist)
                          + math.cos(lat1) * math.sin(ang_dist) * math.cos(bearing))
        lon2 = lon1 + math.atan2(
            math.sin(bearing) * math.sin(ang_dist) * math.cos(lat1),
            math.cos(ang_dist) - math.sin(lat1) * math.sin(lat2),
        )
        points.append((math.degrees(lat2), math.degrees(lon2)))
    return points


def _parse_circular_notam(record: dict, cfg: dict) -> Optional[dict]:
    text = record.get("text", "")
    coord_match = _COORD_RE.search(text)
    if not coord_match:
        LOG.debug("NOTAM %s: no decodable coordinate in text — skipping",
                  record.get("id", "?"))
        return None

    lat = _dms_to_decimal(coord_match["lat_deg"], coord_match["lat_min"],
                           coord_match["lat_sec"], coord_match["lat_hem"])
    lon = _dms_to_decimal(coord_match["lon_deg"], coord_match["lon_min"],
                           coord_match["lon_sec"], coord_match["lon_hem"])

    radius_match = _RADIUS_RE.search(text)
    if radius_match:
        value = float(radius_match["value"])
        unit = radius_match["unit"].upper()
        radius_m = value * _UNIT_TO_METRES[unit]
    else:
        radius_m = cfg["default_radius_m"]
        LOG.debug("NOTAM %s: no radius found in text, defaulting to %.0fm",
                  record.get("id", "?"), radius_m)

    valid_till = record.get("valid_till")
    if not valid_till:
        valid_till = (datetime.now(timezone.utc)
                      + timedelta(hours=cfg["default_validity_hours"])).isoformat()
        LOG.debug("NOTAM %s: no valid_till given, defaulting to +%dh",
                  record.get("id", "?"), cfg["default_validity_hours"])

    notam_id = record.get("id", f"notam:{lat:.4f},{lon:.4f}")
    return {
        "id": f"notam:{notam_id}",
        "name": record.get("id", "NOTAM"),
        "type": "NOTAM",
        "polygon": _circle_polygon(lat, lon, radius_m),
        "verified": False,   # regex-decoded circle is always approximate
        "expiry": valid_till,
        "source": "notam_import",
    }


def _normalise_preformatted(record: dict) -> Optional[dict]:
    polygon = record.get("polygon")
    if not polygon or len(polygon) < 3:
        LOG.warning("Pre-formatted NOTAM record %s has invalid polygon — skipping",
                    record.get("id", "?"))
        return None
    return {
        "id": f"notam:{record.get('id', record.get('name', 'unknown'))}",
        "name": record.get("name", record.get("id", "NOTAM")),
        "type": record.get("type", "NOTAM"),
        "polygon": [tuple(pt) for pt in polygon],
        "verified": bool(record.get("verified", False)),
        "expiry": record.get("expiry"),
        "source": "notam_import",
    }


def _fetch_raw(cfg: dict) -> List[dict]:
    if not cfg.get("url"):
        raise RuntimeError("notam_import: config['notam']['url'] not set — nothing to fetch")
    if requests is None:
        raise RuntimeError("notam_import: 'requests' package not installed")

    resp = requests.get(cfg["url"], timeout=cfg["timeout_s"])
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and "notams" in data:
        data = data["notams"]
    if not isinstance(data, list):
        raise RuntimeError("notam_import: expected a JSON list (or {'notams': [...]})")
    return data


def _load_cache(cache_path: Path) -> List[dict]:
    if not cache_path.exists():
        LOG.warning("notam_import: no cache file at %s — returning empty NOTAM list", cache_path)
        return []
    try:
        return json.loads(cache_path.read_text())
    except (OSError, json.JSONDecodeError):
        LOG.exception("notam_import: failed to read cache file %s", cache_path)
        return []


def _save_cache(cache_path: Path, zones: List[dict]):
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(zones))


def fetch_notams(config: dict) -> List[dict]:
    """
    Public entry point called by airspace_manager. Returns a list of
    zone dicts (matching the schema used throughout the app). Falls
    back to the on-disk cache if the live fetch fails.
    """
    cfg = dict(DEFAULT_CONFIG)
    cfg.update((config or {}).get("notam", {}))
    cache_path = Path(cfg["cache_path"])

    try:
        raw_records = _fetch_raw(cfg)
    except Exception as exc:
        LOG.warning("notam_import: live fetch failed (%s) — falling back to cache", exc)
        return _load_cache(cache_path)

    zones: List[dict] = []
    for record in raw_records:
        try:
            if "polygon" in record:
                zone = _normalise_preformatted(record)
            else:
                zone = _parse_circular_notam(record, cfg)
        except Exception:
            LOG.exception("notam_import: failed to parse record %s — skipping",
                          record.get("id", "?"))
            zone = None

        if zone:
            zones.append(zone)

    LOG.info("notam_import: fetched and parsed %d/%d NOTAM record(s)",
              len(zones), len(raw_records))

    if zones:
        _save_cache(cache_path, zones)
    else:
        LOG.warning("notam_import: fetch succeeded but produced 0 usable zones — "
                    "keeping previous cache untouched")

    return zones


# ----------------------------------------------------------------------
# Standalone smoke test (no network required — uses a synthetic record)
# ----------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)

    sample_records = [
        {
            "id": "A1234/25",
            "text": "AERIAL DISPLAY APRX 2NM RADIUS CENTRE 513026N 0002743W",
            "valid_from": "2025-06-01T00:00:00Z",
            "valid_till": "2025-06-01T18:00:00Z",
        },
        {
            "id": "PREFAB-1",
            "name": "Pre-formatted Test Zone",
            "type": "NOTAM",
            "polygon": [[51.5, -0.1], [51.51, -0.1], [51.51, -0.09], [51.5, -0.09]],
            "verified": True,
            "expiry": None,
        },
    ]

    cfg_stub = {"notam": {"default_radius_m": 3704, "default_validity_hours": 24}}
    parsed = []
    for rec in sample_records:
        z = _normalise_preformatted(rec) if "polygon" in rec else _parse_circular_notam(rec, {**DEFAULT_CONFIG, **cfg_stub["notam"]})
        if z:
            parsed.append(z)

    print(f"Parsed {len(parsed)} zone(s):")
    for z in parsed:
        print(f"  - {z['name']} verified={z['verified']} points={len(z['polygon'])}")
