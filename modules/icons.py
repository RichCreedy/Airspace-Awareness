"""
modules/icons.py
-----------------
Centralized icon constants and path resolution for track markers.

These constants are the values stored in the `icon` field of each
track dict returned by Fusion.get_snapshot()["tracks"], per the
remote-ID merge logic:

  - ICON_DRONE_CROSS_VERIFIED : Remote ID drone seen on BOTH WiFi and
                                 Bluetooth with matching basic_id
                                 ("remoteid_merged" source) — high
                                 confidence.
  - ICON_DRONE_SINGLE_SOURCE  : Remote ID drone seen on only one of
                                 WiFi / Bluetooth — lower confidence.
  - ICON_UNKNOWN              : Track with no basic_id / unclassifiable
                                 passthrough entry.
  - ICON_PLANE_BLUE           : ADS-B manned aircraft.
  - ICON_OWNSHIP              : User's own GPS position.

Keeping these as plain strings (not an Enum) so they serialize cleanly
into snapshot dicts / JSON without extra handling in the GUI or any
future REST/debug endpoints.
"""

import os
import logging

logger = logging.getLogger("icons")

# ---------------------------------------------------------------------
# Icon name constants (stored in track["icon"])
# ---------------------------------------------------------------------

ICON_PLANE_BLUE = "plane_blue"
ICON_DRONE_ORANGE = "drone_orange"
ICON_DRONE_PURPLE = "drone_purple"
ICON_DRONE_CROSS_VERIFIED = "drone_cross_verified"
ICON_DRONE_SINGLE_SOURCE = "drone_single_source"
ICON_UNKNOWN = "unknown"
ICON_OWNSHIP = "ownship"

ALL_ICONS = (
    ICON_PLANE_BLUE,
    ICON_DRONE_ORANGE,
    ICON_DRONE_PURPLE,
    ICON_DRONE_CROSS_VERIFIED,
    ICON_DRONE_SINGLE_SOURCE,
    ICON_UNKNOWN,
    ICON_OWNSHIP,
)

# ---------------------------------------------------------------------
# Icon -> file path mapping
# ---------------------------------------------------------------------
#
# NOTE (⚠️ design decision, please confirm):
# We currently only have 5 actual PNG assets on disk:
#   plane_blue.png, drone_orange.png, drone_purple.png,
#   unknown_grey.png, ownship.png
#
# There is no dedicated "cross-verified badge" asset yet, so:
#   - ICON_DRONE_CROSS_VERIFIED  -> reuses drone_purple.png
#   - ICON_DRONE_SINGLE_SOURCE   -> reuses drone_orange.png
#
# i.e. purple = high-confidence (dual-source verified),
#      orange = single-source / lower confidence.
#
# If you'd rather have a visually distinct "cross/badge overlay" icon
# for verified drones (e.g. drone_purple + small checkmark badge),
# say so and I'll either:
#   (a) generate a composited badge icon at load time using Kivy's
#       canvas, or
#   (b) just flag it as a TODO asset to draw and reference:
#       "images/icons/drone_cross_verified.png"

ICON_BASE_DIR = "images/icons/"

ICON_PATHS = {
    ICON_PLANE_BLUE: os.path.join(ICON_BASE_DIR, "plane_blue.png"),
    ICON_DRONE_ORANGE: os.path.join(ICON_BASE_DIR, "drone_orange.png"),
    ICON_DRONE_PURPLE: os.path.join(ICON_BASE_DIR, "drone_purple.png"),
    ICON_DRONE_CROSS_VERIFIED: os.path.join(ICON_BASE_DIR, "drone_purple.png"),
    ICON_DRONE_SINGLE_SOURCE: os.path.join(ICON_BASE_DIR, "drone_orange.png"),
    ICON_UNKNOWN: os.path.join(ICON_BASE_DIR, "unknown_grey.png"),
    ICON_OWNSHIP: os.path.join(ICON_BASE_DIR, "ownship.png"),
}


def get_icon_path(icon_name: str) -> str:
    """
    Resolve an icon constant to its file path, falling back to the
    "unknown" icon if the name is unrecognized or the asset is
    missing on disk (defensive — avoids Kivy crashing the whole app
    over a missing marker PNG).
    """
    path = ICON_PATHS.get(icon_name)

    if path is None:
        logger.warning(f"[icons] unrecognized icon name '{icon_name}', falling back to unknown")
        path = ICON_PATHS[ICON_UNKNOWN]

    if not os.path.exists(path):
        logger.warning(f"[icons] icon asset missing on disk: {path}, falling back to unknown")
        return ICON_PATHS[ICON_UNKNOWN]

    return path


def validate_icon_assets() -> bool:
    """
    Startup sanity check — verifies every mapped icon file actually
    exists on disk. Call this once at app boot and log a warning
    (not a crash) for any missing assets.
    """
    all_ok = True
    for name, path in ICON_PATHS.items():
        if not os.path.exists(path):
            logger.error(f"[icons] MISSING asset for '{name}': {path}")
            all_ok = False
    return all_ok
