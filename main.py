"""
main.py
-------
uConsole Airspace Awareness Tool — entry point.

Wires together:
  - GPS reader (gpsd / /dev/ttyAMA0)
  - Passive WiFi Remote ID sniffer (scapy, wlan1mon)
  - Bluetooth Remote ID scanner (Bleak)
  - ADS-B poller (tar1090 aircraft.json)
  - Fusion layer (merges tracks, tracks GPS fix, prunes stale data)
  - AirspaceManager (static + remote + manual NOTAM zones, runways, FRZ)
  - ProximityAlert / UnverifiedZoneBanner (GPS-proximity zone warnings)
  - Kivy + kivy_garden.mapview GUI

NOTE: disclaimer_splash.py and freshness_badge.py are intentionally
NOT wired in yet (see TODO markers below) — those are the next patch.
"""

import os
import sys
import logging
import threading
import yaml

from kivy.app import App
from kivy.clock import Clock
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.graphics import Color, Line

from kivy_garden.mapview import MapView, MapMarker, MapLayer

from modules.fusion import Fusion
from modules.gps_reader import GPSReader
from modules.wifi_sniffer import WiFiSniffer
from modules.bluetooth_scanner import BluetoothScanner
from modules.adsb_poller import ADSBPoller
from modules.airspace_manager import AirspaceManager
from modules.unverified_zone_banner import UnverifiedZoneBanner
from modules.icons import get_icon_path, validate_icon_assets, ICON_UNKNOWN, ICON_OWNSHIP
from modules.disclaimer_splash import show_disclaimer_if_needed
from modules.freshness_badge import FreshnessBadge

class AirspaceApp(App):
    def build(self):
        ...
        badge = FreshnessBadge(
            app=self,  # uses DefaultAggregator until sync_metadata.py exists
            retry_callback=lambda: self.airspace_manager.force_resync(),
            poll_interval_s=5.0,
        )
        top_bar.add_widget(badge)
        ...

class AirspaceApp(App):
    def on_start(self):
        # fires after build(), so widget tree/window exists
        show_disclaimer_if_needed(self.root)



logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")

CONFIG_PATH = os.environ.get("AIRSPACE_CONFIG", "config.yaml")


# ---------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------

DEFAULT_CONFIG = {
    "gps": {
        "mode": "gpsd",              # "gpsd" | "serial"
        "serial_port": "/dev/ttyAMA0",
        "baud": 9600,
    },
    "wifi": {
        "enabled": True,
        "interface": "wlan1mon",
    },
    "bluetooth": {
        "enabled": True,
        "adapter": None,
    },
    "adsb": {
        "enabled": True,
        "url": "http://localhost/tar1090/data/aircraft.json",
        "poll_interval_s": 2.0,
    },
    "airspace": {
        "static_zones_path": "data/geofences/uk_zones.geojson",
        "manual_import_dir": "data/geofences/manual_import/",
        "remote_cache_dir": "data/geofences/remote_cache/",
        "refresh_interval_s": 60,
        "backoff_max_s": 900,
        "exclude_airfields": ["Digby"],
        "proximity_alert": {
            "enabled": True,
            "radius_m": 5000,
            "check_interval_s": 15,
        },
    },
    "tiles": {
        "mode": "hybrid",             # "offline" | "online" | "hybrid"
        "offline_dir": "data/tiles/",
    },
}


def load_config(path: str) -> dict:
    if not os.path.exists(path):
        logger.warning(f"[main] config file {path} not found — using defaults")
        return DEFAULT_CONFIG

    with open(path, "r") as f:
        user_cfg = yaml.safe_load(f) or {}

    def deep_merge(base: dict, override: dict) -> dict:
        result = dict(base)
        for k, v in override.items():
            if isinstance(v, dict) and isinstance(result.get(k), dict):
                result[k] = deep_merge(result[k], v)
            else:
                result[k] = v
        return result

    return deep_merge(DEFAULT_CONFIG, user_cfg)


# ---------------------------------------------------------------------
# Icon lookup for track markers
# ---------------------------------------------------------------------

ICON_PATHS = {
    ICON_PLANE_BLUE: "images/icons/plane_blue.png",
    ICON_DRONE_ORANGE: "images/icons/drone_orange.png",
    ICON_DRONE_PURPLE: "images/icons/drone_purple.png",
    ICON_DRONE_CROSS_VERIFIED: "images/icons/drone_purple.png",   # verified badge variant
    ICON_DRONE_SINGLE_SOURCE: "images/icons/drone_orange.png",
    ICON_UNKNOWN: "images/icons/unknown_grey.png",
    ICON_OWNSHIP: "images/icons/ownship.png",
}


# ---------------------------------------------------------------------
# Zone overlay layer — draws geofence polygons on the MapView
# ---------------------------------------------------------------------

class ZoneOverlay(MapLayer):
    """
    Draws zone polygons on top of the map, colored by verified status,
    and exposes hit-testing for tap-to-inspect.

    Verified zones -> solid outline.
    Unverified/approximate zones -> dashed-look outline (approximated
    via alpha) + "⚠️ Approximate FRZ — verified: false" label handled
    by the popup, not drawn permanently on the canvas (keeps the map
    readable at small zoom levels).
    """

    def __init__(self, get_zones_callback, **kwargs):
        super().__init__(**kwargs)
        self.get_zones_callback = get_zones_callback
        self._zones_cache = []

    def reposition(self):
        self.canvas.clear()
        zones = self.get_zones_callback()
        self._zones_cache = zones

        mapview = self.parent
        if mapview is None:
            return

        with self.canvas:
            for zone in zones:
                geometry = zone.get("geometry")
                if not geometry or geometry.get("type") != "Polygon":
                    continue

                verified = zone.get("verified", True)
                color = (0.2, 0.8, 0.2, 0.6) if verified else (0.9, 0.6, 0.0, 0.6)
                Color(*color)

                for ring in geometry.get("coordinates", []):
                    points = []
                    for lon, lat in ring:
                        x, y = mapview.get_window_xy_from(lat, lon, mapview.zoom)
                        points.extend([x, y])
                    if len(points) >= 4:
                        Line(points=points, width=1.5, close=True)

    def point_in_zones(self, lat: float, lon: float) -> list:
        """Returns all cached zones whose polygon contains (lat, lon)."""
        hits = []
        for zone in self._zones_cache:
            geometry = zone.get("geometry")
            if geometry and geometry.get("type") == "Polygon":
                if self._point_in_polygon(lat, lon, geometry["coordinates"][0]):
                    hits.append(zone)
        return hits

    @staticmethod
    def _point_in_polygon(lat, lon, ring) -> bool:
        """Standard ray-casting point-in-polygon test."""
        inside = False
        n = len(ring)
        j = n - 1
        for i in range(n):
            xi, yi = ring[i]
            xj, yj = ring[j]
            if ((yi > lat) != (yj > lat)) and (
                lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-12) + xi
            ):
                inside = not inside
            j = i
        return inside


# ---------------------------------------------------------------------
# Stacked zone popup (shown on tap)
# ---------------------------------------------------------------------

def show_zone_popup(zones: list):
    """
    Scrollable popup listing every zone at the tapped point.
    Each row flags "⚠️ Approximate FRZ — verified: false" when
    zone.get('verified') is False.
    """
    root = BoxLayout(orientation="vertical", size_hint_y=None)
    root.bind(minimum_height=root.setter("height"))

    if not zones:
        root.add_widget(Label(text="No zones at this location.", size_hint_y=None, height=40))
    else:
        for zone in zones:
            verified = zone.get("verified", True)
            name = zone.get("name", "Unnamed Zone")
            category = zone.get("category", "UNKNOWN")

            row = BoxLayout(orientation="vertical", size_hint_y=None, height=70, padding=4)
            row.add_widget(Label(text=f"[b]{name}[/b]  ({category})", markup=True,
                                  size_hint_y=None, height=25))
            if not verified:
                row.add_widget(Label(
                    text="⚠️ Approximate FRZ — verified: false",
                    color=(1, 0.6, 0, 1),
                    size_hint_y=None, height=25,
                ))
            else:
                row.add_widget(Label(text="✅ verified", size_hint_y=None, height=25))
            root.add_widget(row)

    scroll = ScrollView(size_hint=(1, 1))
    scroll.add_widget(root)

    popup = Popup(
        title=f"{len(zones)} zone(s) at this location" if zones else "No zones here",
        content=scroll,
        size_hint=(0.85, 0.7),
    )
    popup.open()


# ---------------------------------------------------------------------
# Main Kivy App
# ---------------------------------------------------------------------

class AirspaceApp(App):
    def __init__(self, config: dict, **kwargs):
        super().__init__(**kwargs)
        self.config_data = config
        self._marker_lookup = {}   # track_id -> MapMarker instance

        # --- Core data modules -------------------------------------
        self.fusion = Fusion()

        self.gps_reader = GPSReader(
            mode=config["gps"]["mode"],
            serial_port=config["gps"]["serial_port"],
            baud=config["gps"]["baud"],
            on_fix=self.fusion.update_gps_fix,
        )

        self.wifi_sniffer = None
        if config["wifi"]["enabled"]:
            self.wifi_sniffer = WiFiSniffer(
                interface=config["wifi"]["interface"],
                on_track=self.fusion.update_wifi_track,
            )

        self.bt_scanner = None
        if config["bluetooth"]["enabled"]:
            self.bt_scanner = BluetoothScanner(
                adapter=config["bluetooth"]["adapter"],
                on_track=self.fusion.update_bt_track,
            )

        self.adsb_poller = None
        if config["adsb"]["enabled"]:
            self.adsb_poller = ADSBPoller(
                url=config["adsb"]["url"],
                poll_interval_s=config["adsb"]["poll_interval_s"],
                on_tracks=self.fusion.update_adsb_tracks,
            )

        self.airspace_manager = AirspaceManager(
            static_zones_path=config["airspace"]["static_zones_path"],
            manual_import_dir=config["airspace"]["manual_import_dir"],
            remote_cache_dir=config["airspace"]["remote_cache_dir"],
            refresh_interval_s=config["airspace"]["refresh_interval_s"],
            backoff_max_s=config["airspace"]["backoff_max_s"],
            exclude_airfields=config["airspace"]["exclude_airfields"],
            on_zones_updated=self.fusion.update_zones,
        )

        self._threads_started = False

    # -------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------

    def build(self):
        self.title = "uConsole Airspace Awareness"

        root = FloatLayout()

        # --- Map --------------------------------------------------
        self.mapview = MapView(zoom=12, lat=51.5, lon=-0.1)
        self.mapview.bind(on_touch_up=self._on_map_touch)
        root.add_widget(self.mapview)

        # --- Zone overlay ------------------------------------------
        self.zone_overlay = ZoneOverlay(get_zones_callback=self.airspace_manager.get_zones)
        self.mapview.add_layer(self.zone_overlay)

        # --- Top bar -------------------------------------------------
        top_bar = BoxLayout(
            orientation="horizontal",
            size_hint=(1, None),
            height=48,
            pos_hint={"top": 1},
        )
        top_bar.add_widget(Label(text="🛰️ Airspace Awareness", bold=True))

        # TODO(freshness_badge): mount FreshnessBadge widget here once
        # freshness_badge.py exists, wired to sync_metadata aggregator.
        top_bar.add_widget(Label(text="[freshness badge placeholder]"))

        retry_btn = Button(text="🔄 Retry Sync", size_hint=(None, 1), width=140)
        retry_btn.bind(on_release=lambda *_: self._on_manual_retry())
        top_bar.add_widget(retry_btn)

        settings_btn = Button(text="⚙️", size_hint=(None, 1), width=48)
        settings_btn.bind(on_release=lambda *_: self._open_settings())
        top_bar.add_widget(settings_btn)

        root.add_widget(top_bar)

        # --- Unverified zone banner (below top bar) -----------------
        prox_cfg = self.config_data["airspace"]["proximity_alert"]
        self.zone_banner = UnverifiedZoneBanner(
            get_zones_callback=self.airspace_manager.get_zones,
            get_gps_fix_callback=self.fusion.get_gps_fix,
            radius_m=prox_cfg["radius_m"],
            check_interval_s=prox_cfg["check_interval_s"],
            enabled=prox_cfg["enabled"],
            on_tap_view_details=self._on_banner_tapped,
            pos_hint={"top": 1 - (48 / self.mapview.height if self.mapview.height else 0.93)},
            size_hint=(1, None),
            height=36,
        )
        root.add_widget(self.zone_banner)

        # TODO(disclaimer_splash): show DisclaimerSplash on first launch
        # (or every launch until "don't show again" is set), before/over
        # this root widget. Wire in main() below once written.

        Clock.schedule_interval(self._update_loop, 1.0)

        return root

    def on_start(self):
        self._start_background_threads()

    def on_stop(self):
        self._stop_background_threads()

    # -------------------------------------------------------------
    # Background threads
    # -------------------------------------------------------------

    def _start_background_threads(self):
        if self._threads_started:
            return

        self.gps_reader.start()

        if self.wifi_sniffer:
            self.wifi_sniffer.start()

        if self.bt_scanner:
            self.bt_scanner.start()

        if self.adsb_poller:
            self.adsb_poller.start()

        self.airspace_manager.start()

        self._threads_started = True
        logger.info("[main] all background modules started")

    def _stop_background_threads(self):
        for mod in (self.gps_reader, self.wifi_sniffer, self.bt_scanner,
                    self.adsb_poller, self.airspace_manager):
            if mod is not None:
                try:
                    mod.stop()
                except Exception as e:
                    logger.warning(f"[main] error stopping {mod}: {e}")
        logger.info("[main] all background modules stopped")

    # -------------------------------------------------------------
    # GUI update loop
    # -------------------------------------------------------------

    def _update_loop(self, dt):
        snapshot = self.fusion.get_snapshot()
        self._update_markers(snapshot["tracks"])
        self._update_ownship(snapshot["gps"])
        self.zone_overlay.reposition()

    def _update_markers(self, tracks: list):
        seen_ids = set()

        for track in tracks:
            track_id = track["id"]
            seen_ids.add(track_id)

            icon_path = get_icon_path(track.get("icon"))   # was: ICON_PATHS.get(track.get("icon"), ICON_PATHS[ICON_UNKNOWN])

            if track_id in self._marker_lookup:
                marker = self._marker_lookup[track_id]
                marker.lat = track["lat"]
                marker.lon = track["lon"]
            else:
                marker = MapMarker(lat=track["lat"], lon=track["lon"], source=icon_path)
                self._marker_lookup[track_id] = marker
                self.mapview.add_marker(marker)

        # Remove markers for tracks that have dropped out (pruned by fusion)
        stale_ids = set(self._marker_lookup.keys()) - seen_ids - {"ownship"}
        for stale_id in stale_ids:
            marker = self._marker_lookup.pop(stale_id)
            self.mapview.remove_marker(marker)

    def _update_ownship(self, gps_fix: dict):
        if not gps_fix or gps_fix.get("lat") is None:
            return

        if "ownship" in self._marker_lookup:
            marker = self._marker_lookup["ownship"]
            marker.lat = gps_fix["lat"]
            marker.lon = gps_fix["lon"]
        else:
            marker = MapMarker(
                lat=gps_fix["lat"], lon=gps_fix["lon"],
                source=get_icon_path(ICON_OWNSHIP)
            )
            self._marker_lookup["ownship"] = marker
            self.mapview.add_marker(marker)

    # -------------------------------------------------------------
    # Interaction handlers
    # -------------------------------------------------------------

    def _on_map_touch(self, mapview, touch):
        if not mapview.collide_point(*touch.pos):
            return False
        if touch.is_double_tap or getattr(touch, "grab_current", None) is not None:
            return False

        # Long-press-free tap detection: only fire if touch didn't drag much.
        if hasattr(touch, "ud") and touch.ud.get("dragged", False):
            return False

        lat, lon = mapview.get_latlon_at(touch.x, touch.y)
        hits = self.zone_overlay.point_in_zones(lat, lon)
        if hits:
            show_zone_popup(hits)
        return False

    def _on_manual_retry(self):
        logger.info("[main] manual sync retry requested from GUI")
        results = self.airspace_manager.force_remote_sync()
        logger.info(f"[main] manual retry results: {results}")

    def _on_banner_tapped(self, nearby_zones: list):
        show_zone_popup(nearby_zones)

    def _open_settings(self):
        # TODO(disclaimer_splash): settings screen should include a
        # "Show safety disclaimer" button that re-triggers the splash
        # on demand, per the persistent "don't show again" requirement.
        logger.info("[main] settings opened (placeholder)")


# ---------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------

def main():
    config = load_config(CONFIG_PATH)
    app = AirspaceApp(config=config)
    app.run()


if __name__ == "__main__":
    main()
