"""
Proximity alert — checks if unverified zones are within a radius
of the current GPS fix using nearest-vertex approximation.
"""
import math
import logging

log = logging.getLogger(__name__)

EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def nearest_vertex_distance_km(lat, lon, geometry):
    """Approximate distance to polygon: min distance to any vertex."""
    coords = _extract_coords(geometry)
    if not coords:
        return float("inf")
    return min(haversine_km(lat, lon, c[1], c[0]) for c in coords)


def _extract_coords(geometry):
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "Polygon":
        return coords[0]
    if gtype == "MultiPolygon":
        pts = []
        for poly in coords:
            pts.extend(poly[0])
        return pts
    return []


def check_unverified_zones_nearby(lat, lon, zones, radius_km):
    """Returns list of (zone, distance_km) for unverified zones within radius."""
    results = []
    for zone in zones:
        if zone.get("verified", False):
            continue
        dist = nearest_vertex_distance_km(lat, lon, zone["geometry"])
        if dist <= radius_km:
            results.append((zone, dist))
    results.sort(key=lambda x: x[1])
    return results
