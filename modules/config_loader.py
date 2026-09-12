"""
modules/config_loader.py — Configuration loading & validation

Responsibilities:
  - Load config.yaml, deep-merge over DEFAULT_CONFIG
  - Validate required fields/types (non-fatal warnings vs fatal errors)
  - Support environment variable overrides (AIRSPACE_<SECTION>_<KEY>)
  - Provide a CLI entry point for standalone config validation:
        python -m modules.config_loader --check config.yaml

Design note: validation deliberately avoids a jsonschema dependency —
the schema here is simple enough to hand-roll and keeps requirements.txt
lean.
"""

from __future__ import annotations

import os
import sys
import copy
import logging
import argparse
from pathlib import Path
from typing import Any

import yaml

LOG = logging.getLogger("config_loader")


class ConfigError(Exception):
    """Raised for fatal configuration problems (missing required fields,
    wrong types for critical settings, etc.)."""
    pass


# ---------------------------------------------------------------------------
# Defaults — every key referenced anywhere in main.py / modules MUST have
# a default here, so a bare-minimum config.yaml (or even an empty one)
# still produces a runnable config.
# ---------------------------------------------------------------------------
DEFAULT_CONFIG: dict[str, Any] = {
    "logging": {
        "level": "INFO",
        "file": "airspace_tool.log",
    },

    "gps": {
        "device": "/dev/ttyAMA0",
        "baud": 9600,
        "fix_timeout_s": 10,
    },

    "wifi": {
        "enabled": True,
        "monitor_interface": None,   # e.g. "wlan1mon" — must be set by user
        "channel_hop": True,
        "channels": [1, 6, 11],
    },

    "bluetooth": {
        "enabled": True,
        "scan_interval_s": 5,
        "adapter": "hci0",
    },

    "adsb": {
        "url": "http://localhost/tar1090/data/aircraft.json",
        "poll_interval_s": 2,
        "stale_after_s": 30,
    },

    "geofences": {
        "path": "data/geofences/uk_zones.geojson",
    },

    "map": {
        "offline_tile_dir": "data/tiles",
        "hybrid_mode": True,          # online w/ offline fallback
        "online_check_interval_s": 30,
        "tile_url_template": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "seed_radius_km": 10,
        "default_zoom": 12,
        "default_center": {"lat": 51.5074, "lon": -0.1278},  # London fallback
    },

    "airspace": {
        "auto_refresh": {
            "enabled": True,
            "interval_minutes": 60,
            "backoff_initial_minutes": 5,
            "backoff_max_minutes": 240,
        },
        "notam": {
            "sources": [],                       # list of fetch URLs
            "import_dir": "data/notam_imports",   # manual drop-in dir
            "expiry_prune_enabled": True,
        },
        "frz": {
            "regen_on_source_change": True,
            "source_dir": "data/geofences/sources",
            "output_path": "data/geofences/uk_zones.geojson",
        },
        "proximity_alert": {
            "enabled": False,
            "radius_m": 5000,
            "check_interval_s": 15,
        },
    },

    "disclaimer": {
        "dont_show_again": False,
        "state_file": "data/.disclaimer_ack",
    },

    "freshness": {
        "warn_after_minutes": 120,
        "error_after_minutes": 720,
    },
}


# ---------------------------------------------------------------------------
# Deep merge helper
# ---------------------------------------------------------------------------
def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge `override` into a copy of `base`. Lists and
    scalars in `override` replace those in `base` outright; dicts merge
    key-by-key."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# Environment variable overrides
#   AIRSPACE_GPS_DEVICE=/dev/ttyUSB0
#   AIRSPACE_AIRSPACE_PROXIMITY_ALERT_ENABLED=true
# ---------------------------------------------------------------------------
def _apply_env_overrides(config: dict, prefix: str = "AIRSPACE_") -> dict:
    """Walk env vars matching AIRSPACE_<PATH>_<TO>_<KEY> and override
    the corresponding nested config value. Best-effort type coercion
    based on the existing default's type."""
    result = copy.deepcopy(config)

    for env_key, env_val in os.environ.items():
        if not env_key.startswith(prefix):
            continue
        path_parts = env_key[len(prefix):].lower().split("_")
        _set_nested_by_fuzzy_path(result, path_parts, env_val)

    return result


def _set_nested_by_fuzzy_path(config: dict, path_parts: list[str], value: str):
    """Attempts to match path_parts against nested dict keys, joining
    parts greedily since env var names can't contain natural delimiters
    for nested keys with underscores in their names (e.g. monitor_interface).
    This is intentionally best-effort — explicit YAML config remains the
    primary/authoritative configuration method."""
    node = config
    parts = path_parts[:]

    while parts:
        matched = False
        # Try longest possible joined key first (handles multi-word keys)
        for length in range(len(parts), 0, -1):
            candidate = "_".join(parts[:length])
            if isinstance(node, dict) and candidate in node:
                if length == len(parts):
                    # Leaf — coerce type based on existing value
                    node[candidate] = _coerce(value, node[candidate])
                    return
                else:
                    node = node[candidate]
                    parts = parts[length:]
                    matched = True
                    break
        if not matched:
            LOG.debug("Env override path did not match config schema: %s",
                      "_".join(path_parts))
            return


def _coerce(raw: str, existing: Any) -> Any:
    if isinstance(existing, bool):
        return raw.lower() in ("1", "true", "yes", "on")
    if isinstance(existing, int):
        try:
            return int(raw)
        except ValueError:
            return existing
    if isinstance(existing, float):
        try:
            return float(raw)
        except ValueError:
            return existing
    return raw  # strings, None, lists — leave as-is


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------
def ensure_config_exists(path: str | Path) -> Path:
    """Create a default config.yaml at `path` if it doesn't exist yet."""
    path = Path(path)
    if not path.exists():
        LOG.warning("No config found at %s — writing default config.", path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            yaml.safe_dump(DEFAULT_CONFIG, f, sort_keys=False)
    return path


def load_config(path: str | Path = "config.yaml",
                 apply_env: bool = True,
                 create_if_missing: bool = True) -> dict:
    """Load config.yaml, merge over defaults, apply env overrides,
    and run validation (raising ConfigError on fatal issues).

    Returns the fully merged, validated config dict.
    """
    path = Path(path)

    if create_if_missing:
        ensure_config_exists(path)
    elif not path.exists():
        raise ConfigError(f"Config file not found: {path}")

    with open(path, "r") as f:
        user_config = yaml.safe_load(f) or {}

    if not isinstance(user_config, dict):
        raise ConfigError(f"Config file {path} did not parse to a mapping "
                           f"(got {type(user_config).__name__})")

    merged = _deep_merge(DEFAULT_CONFIG, user_config)

    if apply_env:
        merged = _apply_env_overrides(merged)

    errors, warnings = validate_config(merged)

    for w in warnings:
        LOG.warning("Config warning: %s", w)

    if errors:
        msg = "\n".join(f"  - {e}" for e in errors)
        raise ConfigError(f"Fatal config errors in {path}:\n{msg}")

    return merged


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate_config(config: dict) -> tuple[list[str], list[str]]:
    """Validate a merged config dict.

    Returns (errors, warnings):
      - errors:   fatal problems — caller should refuse to start
      - warnings: non-fatal problems — log and continue
    """
    errors: list[str] = []
    warnings: list[str] = []

    # --- GPS ---
    gps = config.get("gps", {})
    if not isinstance(gps.get("baud"), int):
        errors.append("gps.baud must be an integer")
    if not gps.get("device"):
        errors.append("gps.device must be set")
    elif not Path(gps["device"]).exists():
        warnings.append(f"gps.device '{gps['device']}' does not exist on "
                         f"this system (ok if testing off-hardware)")

    # --- WiFi ---
    wifi = config.get("wifi", {})
    if wifi.get("enabled") and not wifi.get("monitor_interface"):
        warnings.append("wifi.enabled=true but wifi.monitor_interface is "
                         "unset — passive WiFi detection will be disabled "
                         "at runtime")
    if wifi.get("enabled") and not isinstance(wifi.get("channels"), list):
        errors.append("wifi.channels must be a list of integers")

    # --- Bluetooth ---
    bt = config.get("bluetooth", {})
    if bt.get("enabled") and not isinstance(bt.get("scan_interval_s"), (int, float)):
        errors.append("bluetooth.scan_interval_s must be numeric")

    # --- ADS-B ---
    adsb = config.get("adsb", {})
    if not adsb.get("url", "").startswith(("http://", "https://")):
        errors.append("adsb.url must be a valid http(s) URL")
    if not isinstance(adsb.get("poll_interval_s"), (int, float)) or adsb["poll_interval_s"] <= 0:
        errors.append("adsb.poll_interval_s must be a positive number")

    # --- Geofences ---
    geofences = config.get("geofences", {})
    gpath = geofences.get("path")
    if not gpath:
        errors.append("geofences.path must be set")
    elif not Path(gpath).exists():
        warnings.append(f"geofences.path '{gpath}' does not exist yet — "
                         f"will need to be generated/synced before first use")

    # --- Map ---
    map_cfg = config.get("map", {})
    if map_cfg.get("hybrid_mode") and not Path(map_cfg.get("offline_tile_dir", "")).exists():
        warnings.append(f"map.offline_tile_dir "
                         f"'{map_cfg.get('offline_tile_dir')}' missing — "
                         f"offline fallback unavailable until tiles are seeded")
    center = map_cfg.get("default_center", {})
    if not (_valid_lat(center.get("lat")) and _valid_lon(center.get("lon"))):
        errors.append("map.default_center.lat/lon must be valid coordinates")

    # --- Airspace: auto_refresh ---
    ar = config.get("airspace", {}).get("auto_refresh", {})
    if ar.get("enabled"):
        if not isinstance(ar.get("interval_minutes"), (int, float)) or ar["interval_minutes"] <= 0:
            errors.append("airspace.auto_refresh.interval_minutes must be > 0")
        if ar.get("backoff_max_minutes", 0) < ar.get("interval_minutes", 0):
            warnings.append("airspace.auto_refresh.backoff_max_minutes is "
                             "less than interval_minutes — backoff may "
                             "never actually increase delay")

    # --- Airspace: proximity_alert ---
    pa = config.get("airspace", {}).get("proximity_alert", {})
    if pa.get("enabled"):
        if not isinstance(pa.get("radius_m"), (int, float)) or pa["radius_m"] <= 0:
            errors.append("airspace.proximity_alert.radius_m must be > 0")
        if not isinstance(pa.get("check_interval_s"), (int, float)) or pa["check_interval_s"] <= 0:
            errors.append("airspace.proximity_alert.check_interval_s must be > 0")

    # --- Airspace: notam ---
    notam = config.get("airspace", {}).get("notam", {})
    if notam.get("sources") is not None and not isinstance(notam["sources"], list):
        errors.append("airspace.notam.sources must be a list")
    import_dir = notam.get("import_dir")
    if import_dir and not Path(import_dir).exists():
        warnings.append(f"airspace.notam.import_dir '{import_dir}' does "
                         f"not exist — manual NOTAM imports will fail "
                         f"until created")

    # --- Airspace: frz ---
    frz = config.get("airspace", {}).get("frz", {})
    if frz.get("regen_on_source_change") and not Path(frz.get("source_dir", "")).exists():
        warnings.append(f"airspace.frz.source_dir "
                         f"'{frz.get('source_dir')}' missing — FRZ "
                         f"regeneration has nothing to watch")

    # --- Disclaimer ---
    disclaimer = config.get("disclaimer", {})
    if "state_file" not in disclaimer:
        errors.append("disclaimer.state_file must be set")

    # --- Freshness ---
    fresh = config.get("freshness", {})
    warn_m = fresh.get("warn_after_minutes")
    err_m = fresh.get("error_after_minutes")
    if isinstance(warn_m, (int, float)) and isinstance(err_m, (int, float)):
        if warn_m >= err_m:
            errors.append("freshness.warn_after_minutes must be < "
                           "freshness.error_after_minutes")
    else:
        errors.append("freshness.warn_after_minutes / error_after_minutes "
                       "must be numeric")

    return errors, warnings


def _valid_lat(v: Any) -> bool:
    return isinstance(v, (int, float)) and -90 <= v <= 90


def _valid_lon(v: Any) -> bool:
    return isinstance(v, (int, float)) and -180 <= v <= 180


# ---------------------------------------------------------------------------
# CLI — standalone config validation
#   python -m modules.config_loader --check config.yaml
# ---------------------------------------------------------------------------
def _cli():
    parser = argparse.ArgumentParser(
        description="Validate an airspace-tool config.yaml"
    )
    parser.add_argument("path", nargs="?", default="config.yaml",
                         help="Path to config.yaml (default: config.yaml)")
    parser.add_argument("--check", action="store_true",
                         help="Validate only, don't create missing file")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    path = Path(args.path)
    if not path.exists():
        if args.check:
            print(f"❌ Config file not found: {path}")
            sys.exit(1)
        ensure_config_exists(path)
        print(f"✅ Created default config at {path}")

    with open(path) as f:
        user_config = yaml.safe_load(f) or {}
    merged = _deep_merge(DEFAULT_CONFIG, user_config)
    merged = _apply_env_overrides(merged)

    errors, warnings = validate_config(merged)

    print(f"\n📋 Config validation report: {path}\n" + "-" * 40)
    if not errors and not warnings:
        print("✅ No issues found — config looks complete.")
    if warnings:
        print(f"\n⚠️  {len(warnings)} warning(s):")
        for w in warnings:
            print(f"   - {w}")
    if errors:
        print(f"\n❌ {len(errors)} error(s):")
        for e in errors:
            print(f"   - {e}")

    print()
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    _cli()
