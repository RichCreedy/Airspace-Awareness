"""
tile_manager.py

Hybrid offline/online map tile source. Serves from local cache when
available; fetches + caches from an online tile server when the local
tile is missing AND connectivity is available; auto-detects connectivity
without blocking the GUI thread. Supports bbox and radius pre-seeding.

⚠️ Pre-seeding fetches many tiles from your configured tile server —
respect that server's usage/rate-limit policy; this is on the operator
to verify, not assumed safe by default.
"""
import os
import math
import time
import threading
import logging
import requests

logger = logging.getLogger("tile_manager")

DEFAULT_TILE_DIR = "data/tiles"
DEFAULT_ONLINE_URL_TEMPLATE = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
CONNECTIVITY_PROBE_URL = "https://tile.openstreetmap.org/0/0/0.png"
CONNECTIVITY_CHECK_INTERVAL_S = 60


class TileManager:
    def __init__(self, tile_dir=DEFAULT_TILE_DIR, online_url_template=DEFAULT_ONLINE_URL_TEMPLATE,
                 user_agent="airspace-awareness-tool/1.0 (contact: info@gr8computerservices.co.uk)",
                 connectivity_check_interval_s=CONNECTIVITY_CHECK_INTERVAL_S):
        self.tile_dir = tile_dir
        self.online_url_template = online_url_template
        self.user_agent = user_agent
        self.connectivity_check_interval_s = connectivity_check_interval_s
        self._online = False
        self._last_check = 0
        self._lock = threading.Lock()
        os.makedirs(self.tile_dir, exist_ok=True)

    def _refresh_connectivity(self, force=False):
        now = time.time()
        if not force and (now - self._last_check) < self.connectivity_check_interval_s:
            return self._online
        try:
            resp = requests.get(CONNECTIVITY_PROBE_URL, headers={"User-Agent": self.user_agent}, timeout=3)
            self._online = resp.status_code == 200
        except requests.RequestException:
            self._online = False
        self._last_check = now
        return self._online

    def is_online(self, force_check=False):
        with self._lock:
            return self._refresh_connectivity(force=force_check)

    def _local_path(self, z, x, y):
        return os.path.join(self.tile_dir, str(z), str(x), f"{y}.png")

    def has_local_tile(self, z, x, y):
        return os.path.exists(self._local_path(z, x, y))

    def get_tile(self, z, x, y):
        local_path = self._local_path(z, x, y)
        if os.path.exists(local_path):
            return local_path
        if self.is_online() and self._fetch_and_cache(z, x, y):
            return local_path
        return None

    def _fetch_and_c
