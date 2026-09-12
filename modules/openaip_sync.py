"""
modules/openaip_sync.py — Optional remote sync from OpenAIP

Contract expected by modules/airspace_manager.py:
    fetch_zones(config: dict) -> List[dict]

Returned zone dicts MUST match the shape defined in airspace_manager.py:
    {
        "id": str, "name": str, "type": str,
        "polygon": [(lat, lon), ...],
        "verified": bool, "expiry": str|None, "source": str
    }
(airspace_manager overwrites "source" itself, so it doesn't matter much
what we put there, but we set something sensible anyway.)

This module is OPTIONAL — if `requests` isn't installed, or the API
key isn't configured, fetch_zones() raises a clear exception which
airspace_manager.py logs and continues past.

Config expected under config["remote_sync"], plus top-level:
    remote_sync:
      openaip_enabled: true
      openaip_api_key: "..."
      openaip_bbox: [lat_min, lon_min, lat_max, lon_max]   # optional
      openaip_cache_path: "data/cache/openaip_zones.json"
      openaip_cache_ttl_s: 86400
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

LOG = logging.getLogger("modules.openaip_sync")

try:
    import requests  # type: ignore
except ImportError:
    requests = None  # noqa: N816

OPENAIP_API_BASE = "https://api.openaip.net/api"

# OpenAIP airspace "type" codes we care about, mapped to our internal
# zone "type" field. Extend as needed — anything not in this map is
# skipped (we don't want to flood the map with every ATZ/TMZ variant
# until they've been reviewed).
TYPE_MAP: Dict[int, str] = {
    1: "Restricted",   # Restricted Area
    2: "Danger",        # Danger Area
    5: "Prohibited",    # Prohibited Area
    9: "TMZ",
    14: "RMZ",
}


class OpenAIPError(RuntimeError):
    pass


def _cache_path(config: Dict[str, Any]) -> Path:
    remote_cfg = config.get("remote_sync", {})
    return Path(remote_cfg.get("openaip_cache_path", "data/cache/openaip_zones.json"))


def _load_cache(config: Dict[str, Any]) -> Optional[List[dict]]:
    path = _cache_path(config)
    if not path.exists():
        return None
    remote_cfg = config.get("remote_sync", {})
    ttl_s = float(remote_cfg.get("openaip_cache_ttl_s", 86400))
    age_s = time.time() - path.stat().st_mtime
    if age_s > ttl_s:
        LOG.info("openaip cache expired (%.0fs old, ttl=%.0fs)", age_s, ttl_s)
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        LOG.warning("openaip cache unreadable, ignoring: %s", exc)
        return None


def _save_cache(config: Dict[str, Any], zones: List[dict]):
    path = _cache_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(zones))


def _polygon_from_openaip_geometry(geometry: dict) -> Optional[List[tuple]]:
    """
    OpenAIP airspace geometry is GeoJSON Polygon, [lon, lat] pairs,
    same convention as our other GeoJSON sources.
    """
    coords = (geometry or {}).get("coordinates")
    if not coords:
        return None
    exterior = coords[0]
    try:
        return [(pt[1], pt[0]) for pt in exterior]
    except (IndexError, TypeError):
        return None


def fetch_zones(config: Dict[str, Any]) -> List[dict]:
    """
    Fetch airspace zones from OpenAIP, falling back to on-disk cache
    on network failure. Raises OpenAIPError if there's no usable data
    at all (no network AND no cache) — airspace_manager.py catches
    this and logs it, leaving previously-loaded zones untouched.
    """
    remote_cfg = config.get("remote_sync", {})
    api_key = remote_cfg.get("openaip_api_key")

    if requests is None:
        raise OpenAIPError("requests library not installed")
    if not api_key:
        raise OpenAIPError("remote_sync.openaip_api_key not configured")

    params: Dict[str, Any] = {"apiKey": api_key, "limit": 1000}
    bbox = remote_cfg.get("openaip_bbox")
    if bbox and len(bbox) == 4:
        lat_min, lon_min, lat_max, lon_max = bbox
        params["bbox"] = f"{lon_min},{lat_min},{lon_max},{lat_max}"

    try:
        resp = requests.get(
            f"{OPENAIP_API_BASE}/airspaces", params=params, timeout=15
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        LOG.warning("OpenAIP fetch failed (%s) — trying cache", exc)
        cached = _load_cache(config)
        if cached is not None:
            return cached
        raise OpenAIPError(f"OpenAIP fetch failed and no usable cache: {exc}") from exc

    zones: List[dict] = []
    for item in payload.get("items", payload) if isinstance(payload, dict) else payload:
        type_code = item.get("type")
        zone_type = TYPE_MAP.get(type_code)
        if zone_type is None:
            continue  # not a type we display

        polygon = _polygon_from_openaip_geometry(item.get("geometry"))
        if not polygon:
            continue

        zid = f"openaip:{item.get('_id', item.get('name', 'unknown'))}"
        zones.append({
            "id": zid,
            "name": item.get("name", "Unnamed Airspace"),
            "type": zone_type,
            "polygon": polygon,
            "verified": True,   # OpenAIP data treated as authoritative/verified
            "expiry": None,     # OpenAIP airspaces are permanent, not NOTAM-style
            "source": "openaip",
        })

    if not zones:
        LOG.warning("OpenAIP returned 0 usable zones for configured bbox/filters")

    _save_cache(config, zones)
    return zones
