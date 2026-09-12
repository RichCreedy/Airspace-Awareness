"""
Hybrid map tile sourcing — online when reachable, offline cache fallback.
Supports pre-seeding tiles for a radius around a point.
"""
import os
import logging
import requests

log = logging.getLogger(__name__)


class TileManager:
    def __init__(self, mode, cache_dir, tile_server_url, online_check_url):
        self.mode = mode  # online | offline | hybrid
        self.cache_dir = cache_dir
        self.tile_server_url = tile_server_url
        self.online_check_url = online_check_url
        self._online = None

    def is_online(self, force_check=False):
        if self.mode == "offline":
            return False
        if self.mode == "online":
            return True
        if self._online is not None and not force_check:
            return self._online
        try:
            requests.head(self.online_check_url, timeout=2)
            self._online = True
        except Exception:
            self._online = False
        return self._online

    def get_tile_path(self, z, x, y):
        cache_path = os.path.join(self.cache_dir, str(z), str(x), f"{y}.png")
        if os.path.exists(cache_path):
            return cache_path

        if self.is_online():
            tile_data = self._fetch_online(z, x, y)
            if tile_data:
                self._save_to_cache(cache_path, tile_data)
                return cache_path

        return None  # caller should show placeholder tile

    def _fetch_online(self, z, x, y):
        url = self.tile_server_url.format(z=z, x=x, y=y)
        try:
            resp = requests.get(url, timeout=5)
            resp.raise_for_status()
            return resp.content
        except Exception as e:
            log.debug("Tile fetch failed (%s,%s,%s): %s", z, x, y, e)
            return None

    def _save_to_cache(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)

    def seed_radius(self, lat, lon, radius_km, zoom_levels=(10, 12, 14)):
        """Pre-download tiles around a point for offline use."""
        import math

        for z in zoom_levels:
            n = 2 ** z
            lat_rad = math.radians(lat)
            x_center = int((lon + 180.0) / 360.0 * n)
            y_center = int(
                (1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n
            )
            tile_span = max(1, int(radius_km / 10))
            for x in range(x_center - tile_span, x_center + tile_span + 1):
                for y in range(y_center - tile_span, y_center + tile_span + 1):
                    self.get_tile_path(z, x, y)
        log.info("Seeded tiles around (%s,%s) radius %skm", lat, lon, radius_km)
