"""
estimate_tile_preseed.py
-------------------------
Dry-run estimator for the `map.preseed` block in config.yaml.

Computes the number of OSM-style slippy-map tiles that would be
downloaded for a given center/radius/zoom-level set, and estimates
disk usage — WITHOUT making any network requests.

Usage:
    python3 tools/estimate_tile_preseed.py [path/to/config.yaml]
"""

import sys
import math
import yaml

DEFAULT_CONFIG_PATH = "config.yaml"

# Rough average tile size on disk (PNG, 256x256, typical OSM styling).
# Adjust if your tile source produces denser/larger tiles.
ASSUMED_AVG_TILE_SIZE_KB = 18


def deg2tile(lat_deg: float, lon_deg: float, zoom: int) -> tuple[int, int]:
    """Convert lat/lon to slippy-map tile x/y at a given zoom level."""
    lat_rad = math.radians(lat_deg)
    n = 2 ** zoom
    xtile = int((lon_deg + 180.0) / 360.0 * n)
    ytile = int(
        (1.0 - math.log(math.tan(lat_rad) + (1 / math.cos(lat_rad))) / math.pi)
        / 2.0
        * n
    )
    return xtile, ytile


def bounding_box(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    """
    Approximate a lat/lon bounding box from a center point + radius (km).
    Uses simple equirectangular approximation — fine for tile estimation,
    NOT for precise geodesy.

    Returns (min_lat, max_lat, min_lon, max_lon).
    """
    lat_delta = radius_km / 111.0  # ~111 km per degree latitude
    lon_delta = radius_km / (111.0 * math.cos(math.radians(lat)))
    return (lat - lat_delta, lat + lat_delta, lon - lon_delta, lon + lon_delta)


def count_tiles_for_zoom(min_lat, max_lat, min_lon, max_lon, zoom) -> int:
    """Count tiles covering the bbox at a given zoom level."""
    x_min, y_max = deg2tile(min_lat, min_lon, zoom)   # NB: lower lat -> higher y
    x_max, y_min = deg2tile(max_lat, max_lon, zoom)

    x_lo, x_hi = sorted((x_min, x_max))
    y_lo, y_hi = sorted((y_min, y_max))

    return (x_hi - x_lo + 1) * (y_hi - y_lo + 1)


def estimate(config: dict) -> None:
    preseed = config.get("map", {}).get("preseed", {})
    if not preseed.get("enabled", False):
        print("⚠️  map.preseed.enabled is false — nothing configured to estimate.")
        return

    center_lat = preseed["center_lat"]
    center_lon = preseed["center_lon"]
    radius_km = preseed["radius_km"]
    zoom_levels = preseed["zoom_levels"]

    min_lat, max_lat, min_lon, max_lon = bounding_box(center_lat, center_lon, radius_km)

    print("📍 Pre-seed Estimate")
    print(f"   Center: ({center_lat}, {center_lon})")
    print(f"   Radius: {radius_km} km")
    print(f"   BBox:   lat [{min_lat:.4f}, {max_lat:.4f}], "
          f"lon [{min_lon:.4f}, {max_lon:.4f}]")
    print()
    print(f"{'Zoom':>5} | {'Tiles':>10} | {'Est. Size (MB)':>15}")
    print("-" * 38)

    total_tiles = 0
    for z in zoom_levels:
        tiles = count_tiles_for_zoom(min_lat, max_lat, min_lon, max_lon, z)
        size_mb = (tiles * ASSUMED_AVG_TILE_SIZE_KB) / 1024
        total_tiles += tiles
        print(f"{z:>5} | {tiles:>10} | {size_mb:>15.1f}")

    total_size_mb = (total_tiles * ASSUMED_AVG_TILE_SIZE_KB) / 1024
    print("-" * 38)
    print(f"{'TOTAL':>5} | {total_tiles:>10} | {total_size_mb:>15.1f}")
    print()
    print(f"⚠️  Assumes ~{ASSUMED_AVG_TILE_SIZE_KB} KB/tile average — actual size varies "
          f"by tile provider and rendering density.")
    print("✅ This is a DRY RUN — no tiles were downloaded.")


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CONFIG_PATH
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)
    estimate(config)


if __name__ == "__main__":
    main()
