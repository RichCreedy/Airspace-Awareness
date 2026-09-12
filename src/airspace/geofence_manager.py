"""
Loads and validates GeoJSON geofence zones. Supports hot-reload on mtime change.
"""
import json
import os
import logging

log = logging.getLogger(__name__)


class GeofenceManager:
    def __init__(self, path):
        self.path = path
        self._zones = []
        self._last_mtime = None
        self.reload_if_changed(force=True)

    def reload_if_changed(self, force=False):
        try:
            mtime = os.path.getmtime(self.path)
        except FileNotFoundError:
            log.warning("Geofence file not found: %s", self.path)
            return False

        if not force and mtime == self._last_mtime:
            return False

        with open(self.path, "r") as f:
            data = json.load(f)

        zones = []
        for feature in data.get("features", []):
            props = feature.get("properties", {})
            # Exclude explicitly inactive/non-active runways (e.g., Digby)
            if props.get("active") is False:
                continue
            zones.append({
                "name": props.get("name", "Unnamed Zone"),
                "verified": props.get("verified", False),
                "type": props.get("type", "unknown"),
                "geometry": feature.get("geometry"),
                "properties": props,
            })

        self._zones = zones
        self._last_mtime = mtime
        log.info("Loaded %d geofence zones from %s", len(zones), self.path)
        return True

    def get_zones(self):
        return list(self._zones)

    def zones_at_point(self, lat, lon):
        """Return zones whose polygon contains (lat, lon). Simple ray-cast."""
        hits = []
        for zone in self._zones:
            if self._point_in_geometry(lat, lon, zone["geometry"]):
                hits.append(zone)
        return hits

    @staticmethod
    def _point_in_geometry(lat, lon, geometry):
        if not geometry:
            return False
        gtype = geometry.get("type")
        coords = geometry.get("coordinates")
        if gtype == "Polygon":
            return GeofenceManager._point_in_polygon(lon, lat, coords[0])
        if gtype == "MultiPolygon":
            return any(
                GeofenceManager._point_in_polygon(lon, lat, poly[0])
                for poly in coords
            )
        return False

    @staticmethod
    def _point_in_polygon(x, y, polygon):
        n = len(polygon)
        inside = False
        j = n - 1
        for i in range(n):
            xi, yi = polygon[i]
            xj, yj = polygon[j]
            if ((yi > y) != (yj > y)) and (
                x < (xj - xi) * (y - yi) / (yj - yi + 1e-15) + xi
            ):
                inside = not inside
            j = i
        return inside
