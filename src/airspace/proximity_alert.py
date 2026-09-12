"""
proximity_alert.py
-------------------
Detects unverified airspace zones near the current GPS position.

Design decisions (per project spec):
- Uses NEAREST-VERTEX approximation, not true point-to-polygon-edge
  distance. This is intentionally cheap and conservative-ish, but can
  under/over-estimate proximity near large zones with sparse vertices.
- Zones with `verified: True` are never flagged.
- ⚠️ ASSUMPTION: if the GPS fix is stale (per Fusion's own staleness
  flag), we suppress alerts entirely rather than alerting on a
  possibly-wrong position. Confirm this is desired behavior.
"""

import math
from typing import Optional, List, Dict, Any


EARTH_RADIUS_M = 6_371_000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in meters."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _iter_ring_coords(geometry: Dict[str, Any]):
    """
    Yields (lon, lat) tuples from a GeoJSON-style geometry dict.
    Supports Polygon and MultiPolygon. Silently ignores unknown types.

    ⚠️ ASSUMPTION: coordinates are in GeoJSON order [lon, lat], matching
    `data/geofences/uk_zones.geojson` convention.
    """
    gtype = geometry.get("type")
    coords = geometry.get("coordinates", [])

    if gtype == "Polygon":
        for ring in coords:
            for pt in ring:
                yield pt[0], pt[1]
    elif gtype == "MultiPolygon":
        for polygon in coords:
            for ring in polygon:
                for pt in ring:
                    yield pt[0], pt[1]
    # else: unsupported geometry type -> yields nothing


def _nearest_vertex_distance_m(lat: float, lon: float, geometry: Dict[str, Any]) -> Optional[float]:
    """Returns the distance in meters from (lat, lon) to the nearest
    vertex of the given geometry, or None if geometry has no vertices."""
    best: Optional[float] = None
    for vlon, vlat in _iter_ring_coords(geometry):
        d = haversine_m(lat, lon, vlat, vlon)
        if best is None or d < best:
            best = d
    return best


def check_unverified_zones_nearby(
    gps_fix: Optional[Dict[str, Any]],
    zones: List[Dict[str, Any]],
    radius_m: float = 5000.0,
    ignore_stale_gps: bool = True,
) -> List[Dict[str, Any]]:
    """
    Check for unverified zones within `radius_m` of the current GPS fix.

    Parameters
    ----------
    gps_fix : dict or None
        Expected shape matches `Fusion.get_snapshot()["gps"]`:
        keys `lat`, `lon`, `stale`, etc. May be None if no fix yet.
    zones : list[dict]
        Expected shape matches `Fusion.get_snapshot()["zones"]`:
        each zone dict must have `id`, `name`, `verified` (bool),
        and `geometry` (GeoJSON Polygon/MultiPolygon).
    radius_m : float
        Alert radius in meters. Sourced from
        `config.yaml -> airspace.proximity_alert.radius_m`.
    ignore_stale_gps : bool
        If True (default), returns [] when gps_fix["stale"] is True,
        rather than alerting based on a possibly outdated position.

    Returns
    -------
    list[dict]
        Sorted ascending by distance. Each entry:
        {
            "zone_id": ...,
            "name": ...,
            "distance_m": float,
            "verified": False,
        }
    """
    if not gps_fix:
        return []
    lat, lon = gps_fix.get("lat"), gps_fix.get("lon")
    if lat is None or lon is None:
        return []
    if ignore_stale_gps and gps_fix.get("stale"):
        return []

    results: List[Dict[str, Any]] = []
    for zone in zones:
        if zone.get("verified", False):
            continue
        geometry = zone.get("geometry")
        if not geometry:
            continue

        dist = _nearest_vertex_distance_m(lat, lon, geometry)
        if dist is None:
            continue
        if dist <= radius_m:
            results.append({
                "zone_id": zone.get("id"),
                "name": zone.get("name", "Unnamed zone"),
                "distance_m": dist,
                "verified": False,
            })

    results.sort(key=lambda r: r["distance_m"])
    return results
