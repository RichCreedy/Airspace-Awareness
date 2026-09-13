"""
modules/openaip_sync.py
--------------------------
Remote airspace/NOTAM data sync engine.

Responsibilities:
  - Fetch airspace/NOTAM data from one or more configured remote
    sources (OpenAIP API, NOTAM feeds, or any GeoJSON endpoint).
  - Convert raw responses into the internal zone schema used across
    the app (see ZONE SCHEMA below).
  - Cache successful responses to disk so the app has usable zone
    data even if the network is unavailable at startup.
  - Retry with exponential backoff on failure; never crash the
    background thread on a bad response.
  - Thread-safe: `get_zones()` / `get_status()` can be called from the
    GUI thread at any time without blocking on network I/O.
  - Support both automatic background polling AND manual
    "sync now" triggering (for the freshness badge's retry button).

ZONE SCHEMA (internal, post-conversion):
    {
      "id": str,                # stable unique id, e.g. "openaip:12345"
      "name": str,
      "type": str,               # e.g. "FRZ", "NOTAM", "CTR", "DANGER"
      "verified": bool,          # True only for authoritative sources
      "floor_m": float | None,
      "ceiling_m": float | None,
      "geometry": {...},         # GeoJSON geometry dict (Polygon/MultiPolygon)
      "source": str,             # source name, e.g. "openaip", "notam_uk"
      "raw": dict,                # original raw record, kept for debugging
    }

⚠️ ASSUMPTION: this schema mirrors what I believe `airspace_manager.py`
and `data/geofences/uk_zones.geojson` use. CONFIRM before relying on
merge behavior in airspace_manager.

CONFIG (expected in config.yaml, example):

    airspace:
      remote_sync:
        enabled: true
        cache_dir: "data/cache/openaip"
        default_poll_interval_s: 3600
        max_backoff_s: 3600
        initial_backoff_s: 30
        sources:
          - name: "openaip_airspace_uk"
            enabled: true
            type: "openaip_airspace"          # selects converter
            url: "https://api.core.openaip.net/api/airspaces"
            api_key_env: "e4c323fcf6475153b43adca8a46300ef"     # read from environment
            poll_interval_s: 86400
            params:
              country: "GB"
          - name: "notam_uk_geojson"
            enabled: True
            type: "generic_notam_geojson"
            url: "https://example.invalid/notams.geojson"
            api_key_env: null
            poll_interval_s: 3600

⚠️ ASSUMPTION: exact config key names/nesting under `airspace.remote_sync`
— adjust to match your real config.yaml structure once shared.
"""

import os
import json
import time
import logging
import threading
import requests

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

logger = logging.getLogger("openaip_sync")


# ---------------------------------------------------------------------
# Source configuration
# ---------------------------------------------------------------------

@dataclass
class SourceConfig:
    name: str
    url: str
    type: str                          # selects converter function
    enabled: bool = True
    api_key_env: Optional[str] = None
    poll_interval_s: float = 3600.0
    params: dict = field(default_factory=dict)
    timeout_s: float = 20.0

    def get_api_key(self) -> Optional[str]:
        if not self.api_key_env:
            return None
        key = os.environ.get(self.api_key_env)
        if not key:
            logger.warning(
                f"[openaip_sync] source '{self.name}' expects env var "
                f"'{self.api_key_env}' but it is not set — requests may fail"
            )
        return key


@dataclass
class SourceStatus:
    """Per-source thread-safe-ish status snapshot (protected by parent lock)."""
    last_success_ts: Optional[float] = None
    last_attempt_ts: Optional[float] = None
    last_error: Optional[str] = None
    consecutive_failures: int = 0
    current_backoff_s: float = 0.0
    zone_count: int = 0

    def to_dict(self) -> dict:
        now = time.time()
        age_s = (now - self.last_success_ts) if self.last_success_ts else None
        return {
            "last_sync_iso": (
                datetime.fromtimestamp(self.last_success_ts, tz=timezone.utc).isoformat()
                if self.last_success_ts else None
            ),
            "age_s": age_s,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "zone_count": self.zone_count,
        }


# ---------------------------------------------------------------------
# Converters — raw source-specific response -> internal zone schema
# ---------------------------------------------------------------------
#
# ⚠️ These are BEST-GUESS STUBS. Confirm actual API response shapes
# before trusting conversion output in production.

def _convert_openaip_airspace(raw_response: dict, source_name: str) -> list:
    """
    Convert an OpenAIP /airspaces response into internal zone schema.

    ⚠️ ASSUMPTION: OpenAIP v2 API returns something like:
        { "items": [ { "_id": ..., "name": ..., "type": ...,
                        "geometry": {...}, "lowerLimit": {...},
                        "upperLimit": {...} }, ... ] }
    This is a plausible shape based on public OpenAIP docs but is
    UNVERIFIED against a live response. Adjust field names once you
    have a real sample JSON to test against.
    """
    zones = []
    items = raw_response.get("items", raw_response.get("features", []))
    for item in items:
        try:
            geom = item.get("geometry")
            if geom is None:
                continue
            zone_id = f"openaip:{item.get('_id') or item.get('id')}"
            floor_m = _extract_limit_m(item.get("lowerLimit"))
            ceiling_m = _extract_limit_m(item.get("upperLimit"))
            zones.append({
                "id": zone_id,
                "name": item.get("name", "Unnamed Airspace"),
                "type": str(item.get("type", "UNKNOWN")),
                "verified": True,  # OpenAIP treated as authoritative
                "floor_m": floor_m,
                "ceiling_m": ceiling_m,
                "geometry": geom,
                "source": source_name,
                "raw": item,
            })
        except Exception as e:
            logger.warning(f"[openaip_sync] skipping malformed openaip item: {e}")
    return zones


def _extract_limit_m(limit_obj) -> Optional[float]:
    """
    ⚠️ ASSUMPTION: limit objects look like {"value": 1200, "unit": "FT", "referenceDatum": "MSL"}.
    Converts feet to meters if unit indicates feet; passes through if meters/unknown.
    """
    if not limit_obj or not isinstance(limit_obj, dict):
        return None
    value = limit_obj.get("value")
    unit = str(limit_obj.get("unit", "")).upper()
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if unit in ("FT", "FEET"):
        return value * 0.3048
    return value  # assume already meters


def _convert_generic_notam_geojson(raw_response: dict, source_name: str) -> list:
    """
    Convert a generic GeoJSON FeatureCollection of NOTAM-derived zones
    into internal zone schema. Treated as UNVERIFIED by default since
    manual/community NOTAM sources vary in reliability.

    ⚠️ ASSUMPTION: FeatureCollection features have properties like
    {"id", "name", "floor_m"/"floor_ft", "ceiling_m"/"ceiling_ft"}.
    Adjust per real feed schema.
    """
    zones = []
    features = raw_response.get("features", [])
    for feat in features:
        try:
            props = feat.get("properties", {})
            geom = feat.get("geometry")
            if geom is None:
                continue
            zone_id = f"{source_name}:{props.get('id', hash(json.dumps(props, sort_keys=True)))}"
            floor_m = props.get("floor_m")
            if floor_m is None and "floor_ft" in props:
                floor_m = float(props["floor_ft"]) * 0.3048
            ceiling_m = props.get("ceiling_m")
            if ceiling_m is None and "ceiling_ft" in props:
                ceiling_m = float(props["ceiling_ft"]) * 0.3048
            zones.append({
                "id": zone_id,
                "name": props.get("name", "Unnamed NOTAM Zone"),
                "type": "NOTAM",
                "verified": False,   # unverified by default — flagged in UI
                "floor_m": floor_m,
                "ceiling_m": ceiling_m,
                "geometry": geom,
                "source": source_name,
                "raw": feat,
            })
        except Exception as e:
            logger.warning(f"[openaip_sync] skipping malformed NOTAM feature: {e}")
    return zones


CONVERTERS: dict[str, Callable[[dict, str], list]] = {
    "openaip_airspace": _convert_openaip_airspace,
    "generic_notam_geojson": _convert_generic_notam_geojson,
}


# ---------------------------------------------------------------------
# Main sync engine
# ---------------------------------------------------------------------

class OpenAIPSync:
    """
    Thread-safe multi-source remote airspace/NOTAM sync engine.

    Runs one background thread that polls all enabled sources in turn.
    Each source has its own poll interval and independent exponential
    backoff on failure — a failing source doesn't block others.
    """

    def __init__(
        self,
        sources: list[SourceConfig],
        cache_dir: str = "data/cache/openaip",
        initial_backoff_s: float = 30.0,
        max_backoff_s: float = 3600.0,
        tick_interval_s: float = 10.0,
    ):
        self._sources = sources
        self._cache_dir = cache_dir
        self._initial_backoff_s = initial_backoff_s
        self._max_backoff_s = max_backoff_s
        self._tick_interval_s = tick_interval_s

        self._lock = threading.RLock()
        self._zones_by_source: dict[str, list] = {}
        self._status_by_source: dict[str, SourceStatus] = {
            s.name: SourceStatus() for s in sources
        }
        self._next_attempt_ts: dict[str, float] = {s.name: 0.0 for s in sources}

        self._stop_event = threading.Event()
        self._force_sync_names: set[str] = set()
        self._thread: Optional[threading.Thread] = None

        os.makedirs(self._cache_dir, exist_ok=True)
        self._load_cache_on_init()

    # -- lifecycle ------------------------------------------------------

    def start(self):
        if self._thread and self._thread.is_alive():
            logger.warning("[openaip_sync] start() called but thread already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="openaip_sync")
        self._thread.start()
        logger.info(f"[openaip_sync] started with {len(self._sources)} source(s)")

    def stop(self, timeout_s: float = 5.0):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout_s)
        logger.info("[openaip_sync] stopped")

    def force_resync(self, source_name: Optional[str] = None):
        """
        Manual retry trigger (used by freshness badge's 'Retry Sync Now').
        If source_name is None, forces ALL sources to sync on next tick.
        """
        with self._lock:
            if source_name is None:
                self._force_sync_names.update(s.name for s in self._sources)
            else:
                self._force_sync_names.add(source_name)
        logger.info(f"[openaip_sync] force_resync requested for: {source_name or 'ALL'}")

    # -- public read API --------------------------------------------------

    def get_zones(self) -> list:
        """Return a flat merged list of all zones from all sources (thread-safe copy)."""
        with self._lock:
            merged = []
            for zones in self._zones_by_source.values():
                merged.extend(zones)
            return merged

    def get_status(self) -> dict:
        """
        Thread-safe status snapshot, shaped for freshness_badge's
        DefaultAggregator (see modules/freshness_badge.py):

            {
              "remote_sync": {
                  "age_s": float|None,        # of the FRESHEST successful source
                  "last_error": str|None,     # most recent error across sources, if any
                  "last_sync_iso": str|None,
                  "zone_count": int,          # total merged zone count
              },
              "sources": { source_name: {...per-source detail...}, ... }
            }
        """
        with self._lock:
            per_source = {name: st.to_dict() for name, st in self._status_by_source.items()}

            successful_ages = [
                v["age_s"] for v in per_source.values() if v["age_s"] is not None
            ]
            best_age = min(successful_ages) if successful_ages else None

            errors = [v["last_error"] for v in per_source.values() if v["last_error"]]
            last_error = errors[0] if errors else None

            sync_times = [
                v["last_sync_iso"] for v in per_source.values() if v["last_sync_iso"]
            ]
            last_sync_iso = max(sync_times) if sync_times else None

            total_zones = sum(len(z) for z in self._zones_by_source.values())

            return {
                "remote_sync": {
                    "age_s": best_age,
                    "last_error": last_error,
                    "last_sync_iso": last_sync_iso,
                    "zone_count": total_zones,
                },
                "sources": per_source,
            }

    # -- internals --------------------------------------------------------

    def _cache_path(self, source_name: str) -> str:
        safe_name = source_name.replace("/", "_")
        return os.path.join(self._cache_dir, f"{safe_name}.json")

    def _load_cache_on_init(self):
        """Load any previously cached zones so we're not empty at startup offline."""
        for source in self._sources:
            path = self._cache_path(source.name)
            if os.path.exists(path):
                try:
                    with open(path, "r") as f:
                        cached = json.load(f)
                    zones = cached.get("zones", [])
                    with self._lock:
                        self._zones_by_source[source.name] = zones
                        st = self._status_by_source[source.name]
                        st.last_success_ts = cached.get("cached_ts")
                        st.zone_count = len(zones)
                    logger.info(
                        f"[openaip_sync] loaded {len(zones)} cached zones for "
                        f"'{source.name}' from disk (cache may be stale)"
                    )
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning(f"[openaip_sync] failed to load cache for '{source.name}': {e}")

    def _save_cache(self, source_name: str, zones: list):
        path = self._cache_path(source_name)
        try:
            with open(path, "w") as f:
                json.dump({"cached_ts": time.time(), "zones": zones}, f)
        except OSError as e:
            logger.error(f"[openaip_sync] failed to write cache for '{source_name}': {e}")

    def _run_loop(self):
        while not self._stop_event.is_set():
            now = time.time()
            for source in self._sources:
                if not source.enabled:
                    continue

                with self._lock:
                    forced = source.name in self._force_sync_names
                    due = now >= self._next_attempt_ts.get(source.name, 0)

                if forced or due:
                    self._sync_one(source)
                    with self._lock:
                        self._force_sync_names.discard(source.name)

            self._stop_event.wait(self._tick_interval_s)

    def _sync_one(self, source: SourceConfig):
        with self._lock:
            st = self._status_by_source[source.name]
            st.last_attempt_ts = time.time()

        try:
            raw = self._fetch(source)
            converter = CONVERTERS.get(source.type)
            if converter is None:
                raise ValueError(f"no converter registered for source type '{source.type}'")

            zones = converter(raw, source.name)

            with self._lock:
                self._zones_by_source[source.name] = zones
                st.last_success_ts = time.time()
                st.last_error = None
                st.consecutive_failures = 0
                st.current_backoff_s = 0.0
                st.zone_count = len(zones)
                self._next_attempt_ts[source.name] = time.time() + source.poll_interval_s

            self._save_cache(source.name, zones)
            logger.info(f"[openaip_sync] '{source.name}' synced OK: {len(zones)} zones")

        except Exception as e:
            with self._lock:
                st.last_error = str(e)
                st.consecutive_failures += 1
                backoff = min(
                    self._initial_backoff_s * (2 ** (st.consecutive_failures - 1)),
                    self._max_backoff_s,
                )
                st.current_backoff_s = backoff
                self._next_attempt_ts[source.name] = time.time() + backoff

            logger.error(
                f"[openaip_sync] '{source.name}' sync FAILED "
                f"(attempt {st.consecutive_failures}, next retry in {backoff:.0f}s): {e}"
            )

    def _fetch(self, source: SourceConfig) -> dict:
        headers = {}
        api_key = source.get_api_key()
        if api_key:
            headers["x-openaip-api-key"] = api_key  # ⚠️ ASSUMPTION: header name unconfirmed

        resp = requests.get(
            source.url,
            params=source.params,
            headers=headers,
            timeout=source.timeout_s,
        )
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------
# Factory helper — build from config dict
# ---------------------------------------------------------------------

def build_from_config(config: dict) -> OpenAIPSync:
    """
    Build an OpenAIPSync instance from the `airspace.remote_sync`
    section of the app config.

    ⚠️ ASSUMPTION: config nesting shown in module docstring — adjust
    keys if your real config.yaml differs.
    """
    rs_cfg = config.get("airspace", {}).get("remote_sync", {})
    cache_dir = rs_cfg.get("cache_dir", "data/cache/openaip")
    initial_backoff_s = rs_cfg.get("initial_backoff_s", 30.0)
    max_backoff_s = rs_cfg.get("max_backoff_s", 3600.0)
    default_poll = rs_cfg.get("default_poll_interval_s", 3600.0)

    sources = []
    for s in rs_cfg.get("sources", []):
        sources.append(SourceConfig(
            name=s["name"],
            url=s["url"],
            type=s["type"],
            enabled=s.get("enabled", True),
            api_key_env=s.get("api_key_env"),
            poll_interval_s=s.get("poll_interval_s", default_poll),
            params=s.get("params", {}),
        ))

    return OpenAIPSync(
        sources=sources,
        cache_dir=cache_dir,
        initial_backoff_s=initial_backoff_s,
        max_backoff_s=max_backoff_s,
    )
