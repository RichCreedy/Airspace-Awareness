"""
notam_import.py

Manual NOTAM / temporary restriction import. Full ICAO NOTAM text
parsing is unreliable without a dedicated service, so this supports:

  1. import_manual_entry() — quick circular zone from lat/lon/radius/
     altitude/time-window (the common NOTAM shape).
  2. import_geojson() — import a hand-digitized GeoJSON file.

All imported zones default to verified=False unless the caller
explicitly confirms against an official chart. Includes expiry pruning
via prune_expired().
"""
import json
import os
import uuid
import math
from datetime import datetime, timezone

MANUAL_ZONES_PATH = "data/geofences/manual_zones.geojson"


def _load_manual_zones(path=MANUAL_ZONES_PATH):
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"type": "FeatureCollection", "features": []}


def _save_manual_zones(fc, path=MANUAL_ZONES_PATH):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(fc, f, indent=2)
    os.replace(tmp_path, path)


def _circle_to_polygon(lat, lon, radius_m, n_points=32):
    coords = []
    for i in range(n_points + 1):
        angle = 2 * math.pi * i / n_points
        d_lat = (radius_m / 111320.0) * math.cos(angle)
        d_lon = (radius_m / (111320.0 * math.cos(math.radians(lat)))) * math.sin(angle)
        coords.append([lon + d_lon, lat + d_lat])
    return coords


def import_manual_entry(name, lat, lon, radius_m, lower_alt_ft=0, upper_alt_ft=None,
                         valid_from=None, valid_to=None, notam_ref=None,
                         verified=False, path=MANUAL_ZONES_PATH):
    fc = _load_manual_zones(path)
    feature = {
        "type": "Feature",
        "properties": {
            "id": str(uuid.uuid4()),
            "name": name,
            "source": "manual_import",
            "notam_ref": notam_ref,
            "verified": bool(verified),
            "lower_alt_ft": lower_alt_ft,
            "upper_alt_ft": upper_alt_ft,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "imported_at": datetime.now(timezone.utc).isoformat(),
            "note": "⚠️ Manually imported — verify against official NOTAM/chart",
        },
        "geometry": {"type": "Polygon", "coordinates": [_circle_to_polygon(lat, lon, radius_m)]},
    }
    fc["features"].append(feature)
    _save_manual_zones(fc, path)
    return feature


def import_geojson(source_path, verified=False, notam_ref=None, path=MANUAL_ZONES_PATH):
    with open(source_path, "r") as f:
        incoming = json.load(f)

    fc = _load_manual_zones(path)
    imported = []
    for feat in incoming.get("features", []):
        props = feat.setdefault("properties", {})
        props.setdefault("id", str(uuid.uuid4()))
        props["source"] = "manual_import"
        props["notam_ref"] = notam_ref
        props["verified"] = bool(verified)
        props["imported_at"] = datetime.now(timezone.utc).isoformat()
        props.setdefault("note", "⚠️ Manually imported — verify against official NOTAM/chart")
        fc["features"].append(feat)
        imported.append(feat)

    _save_manual_zones(fc, path)
    return imported


def list_manual_zones(path=MANUAL_ZONES_PATH):
    return _load_manual_zones(path)["features"]


def remove_manual_zone(zone_id, path=MANUAL_ZONES_PATH):
    fc = _load_manual_zones(path)
    before = len(fc["features"])
    fc["features"] = [f for f in fc["features"] if f.get("properties", {}).get("id") != zone_id]
    removed = before - len(fc["features"])
    if removed:
        _save_manual_zones(fc, path)
    return removed > 0


def prune_expired(path=MANUAL_ZONES_PATH, now=None):
    now = now or datetime.now(timezone.utc)
    fc = _load_manual_zones(path)
    kept, removed_count = [], 0
    for feat in fc["features"]:
        valid_to = feat.get("properties", {}).get("valid_to")
        if valid_to:
            try:
                expiry = datetime.fromisoformat(valid_to)
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                if expiry < now:
                    removed_count += 1
                    continue
            except ValueError:
                pass
        kept.append(feat)
    if removed_count:
        fc["features"] = kept
        _save_manual_zones(fc, path)
    return removed_count
