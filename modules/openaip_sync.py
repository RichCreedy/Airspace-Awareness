"""
modules/openaip_sync.py — Remote airspace data sync via openAIP.net

Called by modules/airspace_manager.py as:
    openaip_sync.fetch_zones(config) -> List[zone_dict]

Only invoked when config["remote_sync"]["openaip_enabled"] is True.

ASSUMPTIONS (flagged — confirm against your actual openAIP account):
    - Uses openAIP's public REST API: https://api.core.openaip.net/api
      with a Bearer API key (config["openaip"]["api_key"]).
    - Filters by country=GB by default; optionally further filters to
      a bounding box around a "home" lat/lon + radius, to keep payload
      size sane for a low-power uConsole.
    - openAIP airspace polygons are curated/official data, so these
      zones default to verified=True (unlike our own generated FRZs
      or regex-decoded NOTAMs).
    - openAIP GeoJSON coordinates are [lon, lat] — converted here to
      our internal (lat, lon) convention.
    - Implements simple pagination (openAIP uses `page`/`limit` or
      `offset`/`limit` depending on API version — this uses
      limit/offset, adjust if your API version differs).
    - Implements disk caching (data/cache/openaip_zones.json) with a
      configurable TTL, independent of airspace_manager's own refresh
      interval — avoids hammering the API on every manager refresh
      cycle if that interval is short.
    - On any failure (network, auth, rate limit), falls back to the
      on-disk cache — same hybrid-offline philosophy as map tiles.

Config contract (config["openaip"], all optional except api_key):
    {
        "api_key": None,                       # REQUIRED
        "base_url": "https://api.core.openaip.net/api",
        "country": "GB",
        "home_lat": None,                      # optional bbox centre
        "home_lon": None,
        "bbox_radius_km": 150,                 # only used if home_lat/lon set
        "page_limit": 100,
        "timeout_s": 15,
        "cache_path": "data/cache/openaip_zones.json",
        "cache_ttl_s": 86400,                  # 24h — openAIP data changes rarely
    }
"""

from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

LOG = logging.getLogger("modules.openaip_sync")

try:
    import requests
except ImportError:
    requests = None

DEFAULT_CONFIG: Dict[str, Any] = {
    "api_key": None,
    "base_url": "https://api.core.openaip.net/api",
    "country": "GB",
    "home_lat": None,
    "home_lon": None,
    "bbox_radius_km": 150,
    "page_limit": 100,
    "timeout_s": 15,
    "cache_path": "data/cache/openaip_zones.json",
    "cache_ttl_s": 86400,
}

# openAIP "type" integer codes are numeric in their API; we map a subset
# to human-readable strings for our GUI. Extend as needed.
_TYPE_MAP = {
    0: "Other",
    1: "Restricted",
    2: "Danger",
    3: "Prohibited",
    4: "CTR",
    5: "TMA",
    6: "TMZ",
    7: "TIZ",
    26: "FRZ",  # not an official openAIP code — placeholder, verify
}


def _km_to_deg_lat(km: float) -> float:
    return km / 111.32  # approx km per degree latitude


def _bbox_from_home(lat: float, lon: float, radius_km: float):
    dlat = _km_to_deg_lat(radius_km)
    dlon = radius_km / (111.32 * max(math.cos(math.radians(lat)), 0.01))
    return {
        "min_lat": lat - dlat, "max_lat": lat + dlat,
        "min_lon": lon - dlon, "max_lon": lon + dlon,
    }


def _polygon_from_geojson(geometry: dict) -> Optional[List[tuple]]:
    if not geometry or geometry.get("type") != "Polygon":
        return None
    coords = geometry.get("coordinates")
    if not coords:
        return None
    exterior = coords[0]
    try:
        return [(pt[1], pt[0]) for pt in exterior]  # [lon,lat] -> (lat,lon)
    except (IndexError, TypeError):
        return None


def _load_cache(cache_path: Path) -> Optional[List[dict]]:
    if not cache_path.exists():
        return None
    try:
        payload = json.loads(cache_path.read_text())
        return payload.get("zones")
    except (OSError, json.JSONDecodeError):
        LOG.exception("openaip_sync: failed to read cache file %s", cache_path)
        return None


def _cache_is_fresh(cache_path: Path, ttl_s: float) -> bool:
    if not cache_path.exists():
        return False
    age_s = time.time() - cache_path.stat().st_mtime
    return age_s < ttl_s


def _save_cache(cache_path: Path, zones: List[dict]):
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({"zones": zones, "cached_at": time.time()}))


def _fetch_page(cfg: dict, offset: int) -> dict:
    url = f"{cfg['base_url']}/airspaces"
    params = {
        "country": cfg["country"],
        "limit": cfg["page_limit"],
        "offset": offset,
    }
    if cfg.get("home_lat") is not None and cfg.get("home_lon") is not None:
        bbox = _bbox_from_home(cfg["home_lat"], cfg["home_lon"], cfg["bbox_radius_km"])
        params["bbox"] = f"{bbox['min_lon']},{bbox['min_lat']},{bbox['max_lon']},{bbox['max_lat']}"

    headers = {"x-openaip-api-key": cfg["api_key"]}
    resp = requests.get(url, params=params, headers=headers, timeout=cfg["timeout_s"])
    resp.raise_for_status()
    return resp.json()


def _fetch_all_pages(cfg: dict) -> List[dict]:
    items: List[dict] = []
    offset = 0
    while True:
        payload = _fetch_page(cfg, offset)
        page_items = payload.get("items", payload if isinstance(payload, list) else [])
        if not page_items:
            break
        items.extend(page_items)
        if len(page_items) < cfg["page_limit"]:
            break
        offset += cfg["page_limit"]
        if offset > 5000:  # safety valve against runaway pagination
            LOG.warning("openaip_sync: pagination safety limit reached — stopping early")
            break
    return items


def _normalise_airspace(item: dict) -> Optional[dict]:
    geometry = item.get("geometry")
    polygon = _polygon_from_geojson(geometry)
    if not polygon:
        return None

    type_code = item.get("type")
    type_str = _TYPE_MAP.get(type_code, item.get("icaoClass", "Airspace"))

    name = item.get("name", "Unnamed Airspace")
    zone_id = f"openaip:{item.get('_id', name)}"

    return {
        "id": zone_id,
        "name": name,
        "type": type_str,
        "polygon": polygon,
        "verified": True,   # curated openAIP data — treated as authoritative
        "expiry": None,     # permanent airspace, not time-limited
        "source": "openaip",
    }


def fetch_zones(config: dict) -> List[dict]:
    """
    Public entry point called by airspace_manager. Returns a list of
    zone dicts. Uses a local disk cache (TTL-based) to avoid hammering
    the openAIP API on every airspace_manager refresh cycle, and falls
    back to that cache entirely if the live fetch fails.
    """
    cfg = dict(DEFAULT_CONFIG)
    cfg.update((config or {}).get("openaip", {}))
    cache_path = Path(cfg["cache_path"])

    if _cache_is_fresh(cache_path, cfg["cache_ttl_s"]):
        cached = _load_cache(cache_path)
        if cached is not None:
            LOG.debug("openaip_sync: using fresh cache (%d zones)", len(cached))
            return cached

    if not cfg.get("api_key"):
        LOG.warning("openaip_sync: no api_key configured — falling back to cache (if any)")
        return _load_cache(cache_path) or []

    if requests is None:
        LOG.warning("openaip_sync: 'requests' package not installed — falling back to cache")
        return _load_cache(cache_path) or []

    try:
        raw_items = _fetch_all_pages(cfg)
    except Exception as exc:
        LOG.warning("openaip_sync: live fetch failed (%s) — falling back to cache", exc)
        return _load_cache(cache_path) or []

    zones: List[dict] = []
    for item in raw_items:
        try:
            zone = _normalise_airspace(item)
        except Exception:
            LOG.exception("openaip_sync: failed to normalise airspace item %r",
                          item.get("_id", "?"))
            zone = None
        if zone:
            zones.append(zone)

    LOG.info("openaip_sync: fetched %d raw item(s), normalised %d zone(s)",
              len(raw_items), len(zones))

    if zones:
        _save_cache(cache_path, zones)
    else:
        LOG.warning("openaip_sync: fetch succeeded but produced 0 usable zones — "
                    "keeping previous cache untouched")

    return zones


# ----------------------------------------------------------------------
# Standalone smoke test (no network required — synthetic GeoJSON item)
# ----------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)

    sample_item = {
        "_id": "abc123",
        "name": "LONDON CTR",
        "type": 4,
        "icaoClass": "D",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [-0.5, 51.4], [-0.4, 51.4], [-0.4, 51.5], [-0.5, 51.5], [-0.5, 51.4]
            ]]
        },
    }
    zone = _normalise_airspace(sample_item)
    print("Normalised zone:", zone)
