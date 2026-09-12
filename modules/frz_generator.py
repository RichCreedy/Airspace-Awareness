"""
frz_generator.py

Generates approximate Flight Restriction Zone (FRZ) polygons as circular
buffers around aerodrome reference points — a deliberately approximate
fallback until authoritative boundary data exists.

Generation triggers ONLY when the source aerodrome file's mtime is newer
than the last generated output (see needs_regeneration/regenerate_if_needed)
— never on a fixed timer.

Aerodromes marked `active: false` (e.g. Digby) are excluded entirely.
"""
import json
import os
import uuid
import math
from datetime import datetime, timezone

DEFAULT_AERODROMES_PATH = "data/aerodromes/aerodromes.yaml"
DEFAULT_OUTPUT_PATH = "data/geofences/frz_zones.geojson"
DEFAULT_FRZ_RADIUS_M = 5000

CONFIDENCE_LEVELS = {"high", "medium", "low", "unverified"}


def _circle_to_polygon(lat, lon, radius_m, n_points=48):
    coords = []
    for i in range(n_points + 1):
        angle = 2 * math.pi * i / n_points
        d_lat = (radius_m / 111320.0) * math.cos(angle)
        d_lon = (radius_m / (111320.0 * math.cos(math.radians(lat)))) * math.sin(angle)
        coords.append([lon + d_lon, lat + d_lat])
    return coords


def _load_aerodromes(path):
    import yaml
    with open(path, "r") as f:
        data = yaml.safe_load(f) or {}
    return data.get("aerodromes", [])


def needs_regeneration(source_path=DEFAULT_AERODROMES_PATH, output_path=DEFAULT_OUTPUT_PATH):
    if not os.path.exists(source_path):
        return False
    if not os.path.exists(output_path):
        return True
    return os.path.getmtime(source_path) > os.path.getmtime(output_path)


def generate_frz_zones(source_path=DEFAULT_AERODROMES_PATH, output_path=DEFAULT_OUTPUT_PATH,
                        default_radius_m=DEFAULT_FRZ_RADIUS_M, sync_metadata=None):
    try:
        aerodromes = _load_aerodromes(source_path)
    except (OSError, ValueError) as exc:
        if sync_metadata:
            sync_metadata.record_failure("frz_generation", exc)
        raise

    features, skipped = [], []

    for aerodrome in aerodromes:
        if aerodrome.get("active", True) is False:
            skipped.append(aerodrome.get("icao", aerodrome.get("name", "unknown")))
            continue

        lat, lon = aerodrome.get("lat"), aerodrome.get("lon")
        if lat is None or lon is None:
            continue

        radius_m = aerodrome.get("frz_radius_m", default_radius_m)
        runway_info = []
        for rwy in aerodrome.get("runways", []):
            confidence = rwy.get("heading_confidence", "unverified")
            if confidence not in CONFIDENCE_LEVELS:
                confidence = "unverified"
            runway_info.append({
                "designator": rwy.get("designator"),
                "heading_deg_mag": rwy.get("heading_deg_mag"),
                "heading_confidence": confidence,
                "verify_against_chart": confidence != "high",
            })

        features.append({
            "type": "Feature",
            "properties": {
                "id": aerodrome.get("icao") or str(uuid.uuid4()),
                "name": aerodrome.get("name", "Unknown Aerodrome"),
                "icao": aerodrome.get("icao"),
                "zone_type": "FRZ",
                "source": "auto_generated",
                "verified": False,
                "approximate": True,
                "note": "⚠️ Approximate FRZ — verified: false",
                "generation_method": "circular_buffer_fallback",
                "radius_m": radius_m,
                "runways": runway_info,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
            "geometry": {"type": "Polygon", "coordinates": [_circle_to_polygon(lat, lon, radius_m)]},
        })

    fc = {"type": "FeatureCollection", "features": features}

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    tmp_path = output_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(fc, f, indent=2)
    os.replace(tmp_path, output_path)

    if sync_metadata:
        detail = f"{len(features)} zones generated"
        if skipped:
            detail += f", {len(skipped)} inactive aerodromes skipped ({', '.join(skipped)})"
        sync_metadata.record_success("frz_generation", detail=detail)

    return fc


def regenerate_if_needed(source_path=DEFAULT_AERODROMES_PATH, output_path=DEFAULT_OUTPUT_PATH,
                          sync_metadata=None):
    if needs_regeneration(source_path, output_path):
        return generate_frz_zones(source_path, output_path, sync_metadata=sync_metadata)
    return None
