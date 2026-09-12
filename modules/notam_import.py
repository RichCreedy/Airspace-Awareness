"""
modules/notam_import.py — Optional remote NOTAM sync

Contract expected by modules/airspace_manager.py:
    fetch_notams(config: dict) -> List[dict]

Same zone dict shape as openaip_sync.fetch_zones(). NOTAMs are
typically short-lived, so unlike OpenAIP data they usually carry a
real `expiry` — airspace_manager.py's _prune_expired() will drop them
automatically once expired.

This module targets a generic NOTAM JSON feed (e.g. a self-hosted
proxy in front of the UK NOTAM API, or a third-party aggregator) since
there's no single free/open UK NOTAM API with a stable contract. The
expected feed shape is configurable via `notam_feed_url`, returning:

    {
      "notams": [
        {
          "id": "A1234/24",
          "name": "TEMP DANGER AREA",
          "category": "danger" | "restricted" | "warning" | ...,
          "validFrom": "2024-06-01T00:00:00Z",
          "validTo": "2024-06-10T23:59:00Z",
          "geometry": {"type": "Polygon", "coordinates": [[[lon,lat],...]]}
        },
        ...
      ]
    }

Config expected under config["remote_sync"]:
    remote_sync:
      notam_enabled: true
      notam_feed_url: "https://example.org/notams/uk.json"
      notam_feed_api_key: "..."          # optional, sent as Bearer token
      notam_cache_path: "data/cache/notam_zones.json"
      notam_cache_ttl_s: 3600
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

LOG = logging.getLogger("modules.notam_import")

try:
    import requests  # type: ignore
except ImportError:
    requests = None  # noqa: N816

CATEGORY_MAP: Dict[str, str] = {
    "danger": "Danger",
    "restricted": "Restricted",
    "warning": "NOTAM",
    "prohibited": "Prohibited",
}


class NotamImportError(RuntimeError):
    pass


def _cache_path(config: Dict[str, Any]) -> Path:
    remote_cfg = config.get("remote_sync", {})
    return Path(remote_cfg.get("notam_cache_path", "data/cache/notam_zones.json"))


def _load_cache(config: Dict[str, Any]) -> Optional[List[dict]]:
    path = _cache_path(config)
    if not path.exists():
        return None
    remote_cfg = config.get("remote_sync", {})
    ttl_s = float(remote_cfg.get("notam_cache_ttl_s", 3600))
    age_s = time.time() - path.stat().st_mtime
    if age_s > ttl_s:
        LOG.info("notam cache expired (%.0fs old, ttl=%.0fs)", age_s, ttl_s)
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        LOG.warning("notam cache unreadable, ignoring: %s", exc)
        return None


def _save_cache(config: Dict[str, Any], zones: List[dict]):
    path = _cache_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(zones))


def _polygon_from_notam_geometry(geometry: dict) -> Optional[List[tuple]]:
    coords = (geometry or {}).get("coordinates")
    if not coords:
        return None
    exterior = coords[0]
    try:
        return [(pt[1], pt[0]) for pt in exterior]
    except (IndexError, TypeError):
        return None


def fetch_notams(config: Dict[str, Any]) -> List[dict]:
    """
    Fetch NOTAM-derived zones from the configured feed, falling back
    to cache on failure. Raises NotamImportError if nothing usable is
    available — airspace_manager.py logs and continues past.
    """
    remote_cfg = config.get("remote_sync", {})
    feed_url = remote_cfg.get("notam_feed_url")

    if requests is None:
        raise NotamImportError("requests library not installed")
    if not feed_url:
        raise NotamImportError("remote_sync.notam_feed_url not configured")

    headers = {}
    api_key = remote_cfg.get("notam_feed_api_key")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        resp = requests.get(feed_url, headers=headers, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        LOG.warning("NOTAM feed fetch failed (%s) — trying cache", exc)
        cached = _load_cache(config)
        if cached is not None:
            return cached
        raise NotamImportError(f"NOTAM fetch failed and no usable cache: {exc}") from exc

    zones: List[dict] = []
    for item in payload.get("notams", []):
        polygon = _polygon_from_notam_geometry(item.get("geometry"))
        if not polygon:
            LOG.debug("Skipping NOTAM %s — no usable geometry (text-only NOTAM?)",
                      item.get("id"))
            continue

        category = (item.get("category") or "").lower()
        zone_type = CATEGORY_MAP.get(category, "NOTAM")

        zid = f"notam:{item.get('id', 'unknown')}"
        zones.append({
            "id": zid,
            "name": item.get("name", item.get("id", "NOTAM")),
            "type": zone_type,
            "polygon": polygon,
            "verified": True,          # feed-sourced NOTAMs treated as verified
            "expiry": item.get("validTo"),
            "source": "notam_remote",
        })

    if not zones:
        LOG.info("NOTAM feed returned 0 zones with usable geometry")

    _save_cache(config, zones)
    return zones
