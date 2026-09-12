"""
gui/app.py — Kivy front-end for the uConsole Airspace Awareness Tool

Responsibilities:
    - Safety disclaimer splash (dismissible, persistent "don't show again")
    - Top bar with data freshness badge (tap for per-module detail + retry)
    - Unverified-zone proximity banner (resolves the open "banner or
      counter for unverified zones near GPS" question — implemented as
      a dismissible/snoozable banner, refreshed every 15s)
    - Map view with hybrid online/offline tiles
    - Zone polygon overlay: colour-coded by verified status, with
      "⚠️ Approximate FRZ — verified: false" label on unverified zones
    - Map-tap handling: single zone -> detail popup;
      overlapping zones -> stacked scrollable popup
    - Live aircraft/drone/ownship markers from fusion.get_snapshot()
    - Settings popup: re-show disclaimer, manual airspace retry,
      runway heading list (confidence-flagged, inactive fields excluded)

This module does NOT start/stop backend modules itself — it is handed
a fully-constructed `Orchestrator` instance from main.py and calls into
it (get_module_statuses(), manual_retry_airspace(), fusion, etc.)
"""

from __future__ import annotations

import logging
import time
from math import radians, sin, cos, sqrt, atan2
from pathlib import Path

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.graphics import Color, Line
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.checkbox import CheckBox
from kivy.uix.label import Label
from kivy.uix.modalview import ModalView
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView

from kivy_garden.mapview import MapView, MapMarker, MapLayer, MapSource

LOG = logging.getLogger("gui.app")

# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------
STATE_DIR = Path("data/state")
DISCLAIMER_FLAG_PATH = STATE_DIR / "disclaimer_dismissed.flag"

ICON_MAP = {
    "adsb": "images/icons/plane_blue.png",
    "remoteid_wifi": "images/icons/drone_orange.png",
    "remoteid_bt": "images/icons/drone_purple.png",
    "unknown": "images/icons/unknown_grey.png",
}
OWNSHIP_ICON = "images/icons/ownship.png"

# Airfields excluded from runway heading display (non-active)
RUNWAY_EXCLUDE = {"Digby"}

FRESHNESS_COLOURS = {
    "ok": (0.15, 0.55, 0.15, 1),
    "stale": (0.7, 0.55, 0.0, 1),
    "error": (0.65, 0.15, 0.15, 1),
    "unknown": (0.35, 0.35, 0.35, 1),
}
FRESHNESS_RANK = {"ok": 0, "stale": 1, "error": 2, "unknown": 1}


# ----------------------------------------------------------------------
# Geometry helpers (shared by map-tap handling + zone banner)
# ----------------------------------------------------------------------
def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    R = 6371000
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * R * atan2(sqrt(a), sqrt(1 - a))


def _point_in_polygon(lat, lon, polygon) -> bool:
    """Planar ray-casting test — adequate at UK FRZ scale (few km)."""
    n = len(polygon)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        lat_i, lon_i = polygon[i]
        lat_j, lon_j = polygon[j]
        if (lon_i > lon) != (lon_j > lon):
            slope = (lat_j - lat_i) / (lon_j - lon_i + 1e-12)
            if lat < slope * (lon - lon_i) + lat_i:
                inside = not inside
        j = i
    return inside


def _point_in_or_near_polygon(lat, lon, polygon, tolerance_m=50) -> bool:
    """
    Used for map-tap hit testing. Falls back to a nearest-vertex distance
    check for boundary taps — same approximation strategy already adopted
    for proximity_alert.py, kept consistent rather than doing true
    point-to-edge distance.
    """
    if not polygon:
        return False
    if _point_in_polygon(lat, lon, polygon):
        return True
    return any(_haversine_m(lat, lon, plat, plon) <= tolerance_m for plat, plon in polygon)


# ----------------------------------------------------------------------
# Zone popups (shared by map taps + proximity banner)
# ----------------------------------------------------------------------
def _format_zone_line(zone: dict) -> str:
    verified = zone.get("verified", False)
    flag = "✅ verified" if verified else "⚠️ verified: false"
    name = zone.get("name", "Unnamed zone")
    ztype = zone.get("type", "FRZ")
    return f"{name}  [{ztype}]  {flag}"


def show_zone_detail_popup(zone: dict):
    content = BoxLayout(orientation="vertical", spacing=dp(6), padding=dp(12))
    content.add_widget(Label(
        text=zone.get("name", "Unnamed Zone"), font_size=dp(18), bold=True,
        size_hint_y=None, height=dp(30),
    ))
    content.add_widget(Label(
        text=f"Type: {zone.get('type', '?')}", size_hint_y=None, height=dp(24),
    ))

    verified = zone.get("verified", False)
    if verified:
        warn_text, warn_colour = "✅ Verified boundary", (0.3, 1, 0.3, 1)
    else:
        warn_text, warn_colour = "⚠️ Approximate FRZ — verified: false", (1, 0.6, 0.1, 1)
    content.add_widget(Label(
        text=warn_text, color=warn_colour, bold=True, size_hint_y=None, height=dp(28),
    ))

    if zone.get("expiry"):
        content.add_widget(Label(
            text=f"Expires: {zone['expiry']}", size_hint_y=None, height=dp(22),
        ))
    if zone.get("source"):
        content.add_widget(Label(
            text=f"Source: {zone['source']}", size_hint_y=None, height=dp(22),
        ))

    close_btn = Button(text="Close", size_hint_y=None, height=dp(44))
    content.add_widget(close_btn)

    popup = Popup(title="Zone Info", content=content, size_hint=(0.8, 0.6))
    close_btn.bind(on_release=popup.dismiss)
    popup.open()


def build_zone_list_popup(title: str, zones: list, on_zone_selected=None) -> Popup:
    """
    Stacked, scrollable popup listing multiple zones — used both for
    overlapping-zone map taps and the proximity banner's "view details".
    """
    content = BoxLayout(orientation="vertical", spacing=dp(4), padding=dp(8))
    scroll = ScrollView()
    inner = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(4))
    inner.bind(minimum_height=inner.setter("height"))

    for zone in zones:
        btn = Button(text=_format_zone_line(zone), size_hint_y=None, height=dp(56))
        if on_zone_selected:
            btn.bind(on_release=lambda _inst, z=zone: on_zone_selected(z))
        inner.add_widget(btn)

    scroll.add_widget(inner)
    content.add_widget(scroll)

    close_btn = Button(text="Close", size_hint_y=None, height=dp(44))
    content.add_widget(close_btn)

    popup = Popup(title=title, content=content, size_hint=(0.9, 0.8))
    close_btn.bind(on_release=popup.dismiss)
    popup.open()
    return popup


def show_runway_popup(orchestrator):
    """
    Runway headings with confidence indicators. Inactive airfields
    (e.g. Digby) are excluded per project decision.

    ⚠️ ASSUMPTION: relies on orchestrator.airspace_manager.get_runways()
    returning [{airfield, runway, heading_true, confidence, verified}, ...].
    This accessor does not exist yet in the (unwritten) AirspaceManager —
    flagged for follow-up when that module is built.
    """
    runways = []
    try:
        runways = orchestrator.airspace_manager.get_runways()
    except AttributeError:
        LOG.warning("AirspaceManager.get_runways() not implemented yet")

    runways = [r for r in runways if r.get("airfield") not in RUNWAY_EXCLUDE]

    content = BoxLayout(orientation="vertical", padding=dp(8), spacing=dp(4))
    scroll = ScrollView()
    inner = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(2))
    inner.bind(minimum_height=inner.setter("height"))

    if not runways:
        inner.add_widget(Label(text="No runway data available yet.",
                                size_hint_y=None, height=dp(40)))
    else:
        conf_icons = {"high": "✅", "medium": "🟡", "low": "🔶"}
        for rw in runways:
            icon = conf_icons.get(rw.get("confidence", "unknown"), "❓")
            flag = "" if rw.get("verified", False) else "  (unverified — flagged for review)"
            line = (f"{rw.get('airfield', '?')} RWY {rw.get('runway', '?')}: "
                    f"{rw.get('heading_true', '?')}° {icon}{flag}")
            inner.add_widget(Label(text=line, size_hint_y=None, height=dp(32)))

    scroll.add_widget(inner)
    content.add_widget(scroll)

    close_btn = Button(text="Close", size_hint_y=None, height=dp(44))
    content.add_widget(close_btn)

    popup = Popup(title="Runway Headings", content=content, size_hint=(0.9, 0.85))
    close_btn.bind(on_release=popup.dismiss)
    popup.open()


# ----------------------------------------------------------------------
# Safety disclaimer splash
# ----------------------------------------------------------------------
class DisclaimerSplash(ModalView):
    """
    Must be actively dismissed on first run (auto_dismiss=False).
    Offers a persistent "don't show again" checkbox (writes a flag file)
    and remains re-openable from Settings regardless of that flag.
    """

    DISCLAIMER_TEXT = (
        "This tool provides best-effort situational awareness only, "
        "combining passive detection, ADS-B, and unverified geofence data.\n\n"
        "It MUST NOT be used as a sole means of airspace deconfliction, "
        "collision avoidance, or regulatory compliance. Zone boundaries "
        "marked 'unverified' are approximate. Always comply with official "
        "NOTAMs, ANO restrictions, and CAA guidance.\n\n"
        "Questions or corrections: info@gr8computerservices.co.uk"
    )

    def __init__(self, **kwargs):
        super().__init__(auto_dismiss=False, size_hint=(0.9, 0.75), **kwargs)
        layout = BoxLayout(orientation="vertical", spacing=dp(10), padding=dp(16))

        layout.add_widget(Label(
            text="⚠️ Safety Disclaimer", font_size=dp(20), bold=True,
            size_hint_y=None, height=dp(36),
        ))

        scroll = ScrollView()
        body = Label(
            text=self.DISCLAIMER_TEXT, size_hint_y=None,
            halign="left", valign="top", padding=(dp(4), dp(4)),
        )
        body.bind(width=lambda w, val: setattr(w, "text_size", (val, None)))
        body.bind(texture_size=lambda w, val: setattr(w, "height", val[1]))
        scroll.add_widget(body)
        layout.add_widget(scroll)

        checkbox_row = BoxLayout(size_hint_y=None, height=dp(36), spacing=dp(8))
        self._dont_show_checkbox = CheckBox(size_hint_x=None, width=dp(32))
        checkbox_row.add_widget(self._dont_show_checkbox)
        checkbox_row.add_widget(Label(text="Don't show this again"))
        layout.add_widget(checkbox_row)

        confirm_btn = Button(text="I Understand — Continue", size_hint_y=None, height=dp(48))
        confirm_btn.bind(on_release=self._on_confirm)
        layout.add_widget(confirm_btn)

        self.add_widget(layout)

    def _on_confirm(self, *args):
        if self._dont_show_checkbox.active:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            DISCLAIMER_FLAG_PATH.write_text("dismissed")
            LOG.info("Disclaimer dismissed permanently by user")
        else:
            LOG.info("Disclaimer dismissed for this session only")
        self.dismiss()

    @staticmethod
    def should_show() -> bool:
        return not DISCLAIMER_FLAG_PATH.exists()

    @staticmethod
    def reset():
        if DISCLAIMER_FLAG_PATH.exists():
            DISCLAIMER_FLAG_PATH.unlink()
            LOG.info("Disclaimer flag reset — will show again on next open")


# ----------------------------------------------------------------------
# Data freshness badge
# ----------------------------------------------------------------------
class FreshnessBadge(Button):
    """
    Top-bar widget showing the worst-case status across all backend
    modules. Tap opens a detail popup with per-module last-sync time,
    error text, and a manual retry button for airspace sync.
    """

    def __init__(self, orchestrator, **kwargs):
        super().__init__(**kwargs)
        self.orchestrator = orchestrator
        self.text = "Sync: —"
        self.background_normal = ""
        self.background_color = FRESHNESS_COLOURS["unknown"]
        self.bind(on_release=self.show_details)
        Clock.schedule_interval(self._refresh, 5)
        Clock.schedule_once(self._refresh, 0.5)

    def _refresh(self, dt):
        try:
            statuses = self.orchestrator.get_module_statuses()
        except Exception:
            LOG.exception("Failed to fetch module statuses")
            return
        worst = self._worst_state(statuses)
        self.text = f"Sync: {worst.upper()}"
        self.background_color = FRESHNESS_COLOURS.get(worst, FRESHNESS_COLOURS["unknown"])

    @staticmethod
    def _worst_state(statuses: dict) -> str:
        worst = "ok"
        for status in statuses.values():
            state = status.get("state", "unknown")
            if FRESHNESS_RANK.get(state, 1) > FRESHNESS_RANK.get(worst, 0):
                worst = state
        return worst

    def show_details(self, *args):
        statuses = self.orchestrator.get_module_statuses()
        content = BoxLayout(orientation="vertical", spacing=dp(6), padding=dp(8))
        scroll = ScrollView()
        inner = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(6))
        inner.bind(minimum_height=inner.setter("height"))

        for name, status in statuses.items():
            row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
            state = status.get("state", "unknown")
            last_sync = status.get("last_sync", "never")
            error = status.get("error")
            text = f"{name}: {state.upper()} (last: {last_sync})"
            if error:
                text += f"\n  ⚠ {error}"
            row.add_widget(Label(text=text, halign="left"))
            if name == "airspace" and state == "error":
                retry_btn = Button(text="Retry", size_hint_x=None, width=dp(80))
                retry_btn.bind(on_release=lambda *_: self.orchestrator.manual_retry_airspace())
                row.add_widget(retry_btn)
            inner.add_widget(row)

        scroll.add_widget(inner)
        content.add_widget(scroll)
        close_btn = Button(text="Close", size_hint_y=None, height=dp(44))
        content.add_widget(close_btn)

        popup = Popup(title="Data Freshness", content=content, size_hint=(0.85, 0.7))
        close_btn.bind(on_release=popup.dismiss)
        popup.open()


# ----------------------------------------------------------------------
# Unverified-zone proximity banner
# (resolves open question: "banner or counter for unverified zones
#  near GPS position" — implemented as a dismissible/snoozable banner)
# ----------------------------------------------------------------------
class UnverifiedZoneBanner(BoxLayout):
    """
    Refreshed every 15s per config. Shows a count of unverified zones
    within `airspace.proximity_alert.radius_m` of the current GPS fix.
    Tapping the text opens the stacked zone list; the ✕ button snoozes
    the banner for 5 minutes.

    ⚠️ ASSUMPTION: fusion.get_snapshot()["zones"] entries include a
    `distance_from_fix_m` field, precomputed by ProximityAlertMonitor.
    If fusion instead only stores raw polygons, this field needs to be
    computed here from gps_fix + polygon centroid as a fallback.
    """

    def __init__(self, fusion, config: dict, **kwargs):
        super().__init__(orientation="horizontal", height=0, size_hint_y=None, **kwargs)
        self.fusion = fusion
        prox_cfg = (config.get("airspace", {}) or {}).get("proximity_alert", {}) or {}
        self.enabled = prox_cfg.get("enabled", True)
        self.radius_m = prox_cfg.get("radius_m", 2000)
        self._snoozed_until = 0.0
        self._zones_cache: list = []
        self.opacity = 0

        self.label_widget = Button(
            text="", halign="left",
            background_normal="", background_color=(0.35, 0.18, 0.0, 0.9),
        )
        self.label_widget.bind(on_release=self._show_zone_list)
        self.add_widget(self.label_widget)

        dismiss_btn = Button(text="✕", size_hint_x=None, width=dp(40),
                              background_normal="", background_color=(0.35, 0.18, 0.0, 0.9))
        dismiss_btn.bind(on_release=self._snooze)
        self.add_widget(dismiss_btn)

        if self.enabled:
            Clock.schedule_interval(self._refresh, 15)
            Clock.schedule_once(self._refresh, 1.0)

    def _refresh(self, dt=None):
        if not self.enabled or time.time() < self._snoozed_until:
            return
        try:
            snapshot = self.fusion.get_snapshot()
        except Exception:
            LOG.exception("Failed to get fusion snapshot for zone banner")
            return

        if not snapshot.get("gps_fix"):
            self._hide()
            return

        unverified_nearby = [
            z for z in snapshot.get("zones", [])
            if not z.get("verified", False)
            and z.get("distance_from_fix_m") is not None
            and z["distance_from_fix_m"] <= self.radius_m
        ]

        if unverified_nearby:
            self._zones_cache = unverified_nearby
            self.height = dp(36)
            self.opacity = 1
            self.label_widget.text = (
                f"⚠️ {len(unverified_nearby)} unverified zone(s) within "
                f"{self.radius_m}m — tap for details"
            )
        else:
            self._hide()

    def _hide(self):
        self.height = 0
        self.opacity = 0

    def _show_zone_list(self, *args):
        if self._zones_cache:
            build_zone_list_popup(
                "Nearby Unverified Zones", self._zones_cache,
                on_zone_selected=show_zone_detail_popup,
            )

    def _snooze(self, *args):
        self._snoozed_until = time.time() + 300
        self._hide()


# ----------------------------------------------------------------------
# Zone polygon overlay layer
# ----------------------------------------------------------------------
class ZoneOverlayLayer(MapLayer):
    """
    Draws geofence zone polygons on the map, colour-coded by verified
    status, with a child Label reading "⚠️ Approximate FRZ — verified:
    false" positioned at the centroid of each unverified zone.

    ⚠️ ASSUMPTION: MapView exposes get_window_xy_from(lat, lon, zoom)
    returning widget-local pixel coordinates. Confirmed API shape on
    kivy_garden.mapview ~1.0.x — re-check against the pinned version.
    """

    def __init__(self, fusion, **kwargs):
        super().__init__(**kwargs)
        self.fusion = fusion
        self._warning_labels: dict = {}

    def reposition(self):
        mapview = self.parent
        if mapview is None:
            return

        self.canvas.clear()
        try:
            snapshot = self.fusion.get_snapshot()
        except Exception:
            LOG.exception("Failed to get fusion snapshot for zone overlay")
            return

        zones = snapshot.get("zones", [])
        seen_ids = set()

        with self.canvas:
            for zone in zones:
                zid = zone.get("id") or zone.get("name")
                pts = self._project_polygon(mapview, zone.get("polygon", []))
                if not pts:
                    continue
                seen_ids.add(zid)

                verified = zone.get("verified", False)
                if verified:
                    Color(0.1, 0.75, 0.2, 0.9)
                else:
                    Color(1, 0.45, 0.0, 0.9)
                Line(points=pts + pts[:2], width=dp(2), close=True)

                if not verified:
                    self._update_warning_label(zid, pts)

        for zid in list(self._warning_labels.keys()):
            if zid not in seen_ids:
                self.remove_widget(self._warning_labels.pop(zid))

    def _project_polygon(self, mapview, coords):
        if not coords or len(coords) < 3:
            return None
        pts = []
        for lat, lon in coords:
            try:
                x, y = mapview.get_window_xy_from(lat, lon, mapview.zoom)
            except Exception:
                return None
            pts.extend([x, y])
        return pts

    def _update_warning_label(self, zid, pts):
        cx = sum(pts[0::2]) / (len(pts) // 2)
        cy = sum(pts[1::2]) / (len(pts) // 2)
        label = self._warning_labels.get(zid)
        if label is None:
            label = Label(
                text="⚠️ Approximate FRZ — verified: false",
                size_hint=(None, None), size=(dp(230), dp(20)),
                color=(1, 0.6, 0.1, 1), font_size=dp(11), bold=True,
            )
            self._warning_labels[zid] = label
            self.add_widget(label)
        label.center = (cx, cy)


# ----------------------------------------------------------------------
# Hybrid online/offline tile source
# ----------------------------------------------------------------------
class HybridMapSource(MapSource):
    """
    Placeholder wiring point for hybrid tile sourcing (online when
    reachable, offline cache fallback otherwise). garden.mapview already
    does file-based caching via `cache_dir`; true "switch to offline
    when no network" logic depends on internals that vary by mapview
    release, so the real switch should live in a dedicated
    modules/tile_provider.py (not yet written). For now this subclass
    just points at the pre-seeded cache_dir and online URL template.
    """

    def __init__(self, online_url_template: str, cache_dir="data/tiles", **kwargs):
        super().__init__(url=online_url_template, cache_dir=cache_dir, **kwargs)


# ----------------------------------------------------------------------
# Map view with tap handling + live markers
# ----------------------------------------------------------------------
class AirspaceMapView(MapView):
    """
    MapView subclass adding:
      - tap-to-query zone popups (single zone -> detail; overlapping
        zones at the tap point -> stacked scrollable popup)
      - live aircraft/drone track markers from fusion snapshot
      - ownship marker + auto-recenter on GPS fix
    """

    def __init__(self, fusion, **kwargs):
        super().__init__(**kwargs)
        self.fusion = fusion
        self._track_markers: dict = {}
        self._ownship_marker = None
        self._zone_layer = ZoneOverlayLayer(fusion)
        self.add_layer(self._zone_layer)

    def on_touch_up(self, touch):
        handled = super().on_touch_up(touch)
        if not self.collide_point(*touch.pos):
            return handled
        if touch.ud.get("mapview_pan", False):
            # MapView marks pan gestures on touch.ud; avoid treating a
            # drag-release as a tap. Attribute name is best-effort —
            # confirm against installed garden.mapview version.
            return handled

        try:
            lat, lon = self.get_latlon_at(touch.x, touch.y)
        except Exception:
            LOG.exception("Failed to resolve tap coordinates")
            return handled

        self._handle_zone_tap(lat, lon)
        return handled

    def _handle_zone_tap(self, lat, lon):
        try:
            snapshot = self.fusion.get_snapshot()
        except Exception:
            LOG.exception("Failed to get fusion snapshot on map tap")
            return

        zones = snapshot.get("zones", [])
        hits = [
            z for z in zones
            if _point_in_or_near_polygon(lat, lon, z.get("polygon", []), tolerance_m=50)
        ]
        if not hits:
            return
        if len(hits) == 1:
            show_zone_detail_popup(hits[0])
        else:
            build_zone_list_popup(
                "Overlapping Zones", hits, on_zone_selected=show_zone_detail_popup
            )

    def update_tracks(self, snapshot: dict):
        seen = set()
        for track in snapshot.get("tracks", []):
            tid = track.get("id")
            if tid is None or track.get("lat") is None or track.get("lon") is None:
                continue
            seen.add(tid)
            icon = ICON_MAP.get(track.get("source", "unknown"), ICON_MAP["unknown"])
            marker = self._track_markers.get(tid)
            if marker is None:
                marker = MapMarker(lat=track["lat"], lon=track["lon"], source=icon)
                self._track_markers[tid] = marker
                self.add_marker(marker)
            else:
                marker.lat, marker.lon = track["lat"], track["lon"]
                if marker.source != icon:
                    marker.source = icon

        for tid in list(self._track_markers.keys()):
            if tid not in seen:
                self.remove_marker(self._track_markers.pop(tid))

    def update_ownship(self, gps_fix: dict | None):
        if not gps_fix or gps_fix.get("lat") is None:
            return
        if self._ownship_marker is None:
            self._ownship_marker = MapMarker(
                lat=gps_fix["lat"], lon=gps_fix["lon"], source=OWNSHIP_ICON
            )
            self.add_marker(self._ownship_marker)
        else:
            self._ownship_marker.lat = gps_fix["lat"]
            self._ownship_marker.lon = gps_fix["lon"]
        self.center_on(gps_fix["lat"], gps_fix["lon"])

    def center_on(self, lat, lon):
        self.lat = lat
        self.lon = lon


# ----------------------------------------------------------------------
# Settings popup
# ----------------------------------------------------------------------
class SettingsPopup(Popup):
    def __init__(self, app: "AirspaceToolApp", **kwargs):
        super().__init__(title="Settings", size_hint=(0.85, 0.85), **kwargs)
        self.app_ref = app

        content = BoxLayout(orientation="vertical", spacing=dp(10), padding=dp(12))
        content.add_widget(Label(
            text="Airspace Awareness Tool", font_size=dp(18), bold=True,
            size_hint_y=None, height=dp(28),
        ))
        content.add_widget(Label(
            text="Contact: info@gr8computerservices.co.uk",
            font_size=dp(12), size_hint_y=None, height=dp(20),
        ))

        disclaimer_btn = Button(text="Show Safety Disclaimer Again",
                                 size_hint_y=None, height=dp(44))
        disclaimer_btn.bind(on_release=self._show_disclaimer_again)
        content.add_widget(disclaimer_btn)

        retry_btn = Button(text="Retry Airspace Sync Now",
                            size_hint_y=None, height=dp(44))
        retry_btn.bind(on_release=lambda *_: app.orchestrator.manual_retry_airspace())
        content.add_widget(retry_btn)

        runway_btn = Button(text="Runway Headings", size_hint_y=None, height=dp(44))
        runway_btn.bind(on_release=lambda *_: show_runway_popup(app.orchestrator))
        content.add_widget(runway_btn)

        close_btn = Button(text="Close", size_hint_y=None, height=dp(44))
        close_btn.bind(on_release=self.dismiss)
        content.add_widget(close_btn)

        self.content = content

    def _show_disclaimer_again(self, *args):
        DisclaimerSplash.reset()
        self.dismiss()
        DisclaimerSplash().open()


# ----------------------------------------------------------------------
# Main App
# ----------------------------------------------------------------------
class AirspaceToolApp(App):
    title = "Airspace Awareness"

    def __init__(self, orchestrator, config: dict, **kwargs):
        super().__init__(**kwargs)
        self.orchestrator = orchestrator
        self.config = config
        self.fusion = orchestrator.fusion
        self._update_event = None

    def build(self):
        Window.clearcolor = (0.05, 0.05, 0.05, 1)
        root = BoxLayout(orientation="vertical")

        # --- Top bar ---
        top_bar = BoxLayout(size_hint_y=None, height=dp(48), padding=dp(4), spacing=dp(4))
        top_bar.add_widget(Label(text="Airspace Awareness", bold=True))
        self.freshness_badge = FreshnessBadge(self.orchestrator, size_hint_x=None, width=dp(140))
        settings_btn = Button(text="⚙", size_hint_x=None, width=dp(48))
        settings_btn.bind(on_release=lambda *_: SettingsPopup(self).open())
        top_bar.add_widget(self.freshness_badge)
        top_bar.add_widget(settings_btn)
        root.add_widget(top_bar)

        # --- Proximity banner ---
        self.zone_banner = UnverifiedZoneBanner(self.fusion, self.config)
        root.add_widget(self.zone_banner)

        # --- Map ---
        map_cfg = self.config.get("map", {}) or {}
        map_source = self._build_map_source(map_cfg)
        self.map_view = AirspaceMapView(
            fusion=self.fusion,
            zoom=map_cfg.get("default_zoom", 13),
            lat=map_cfg.get("default_lat", 53.0),
            lon=map_cfg.get("default_lon", -1.5),
            map_source=map_source,
        )
        root.add_widget(self.map_view)

        # --- First-run disclaimer ---
        if DisclaimerSplash.should_show():
            Clock.schedule_once(lambda dt: DisclaimerSplash().open(), 0.3)

        # --- Periodic refresh from fusion snapshot ---
        self._update_event = Clock.schedule_interval(self._refresh_from_fusion, 1.0)

        return root

    def _build_map_source(self, map_cfg: dict):
        mode = map_cfg.get("mode", "hybrid")
        online_url = map_cfg.get(
            "online_url", "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
        )
        cache_dir = map_cfg.get("cache_dir", "data/tiles")

        if mode == "offline":
            return MapSource(url="", cache_dir=cache_dir)
        if mode == "online":
            return MapSource(url=online_url, cache_dir=cache_dir)
        return HybridMapSource(online_url, cache_dir=cache_dir)

    def _refresh_from_fusion(self, dt):
        try:
            snapshot = self.fusion.get_snapshot()
        except Exception:
            LOG.exception("Failed to refresh GUI from fusion snapshot")
            return

        self.map_view.update_tracks(snapshot)
        self.map_view.update_ownship(snapshot.get("gps_fix"))
        self.map_view._zone_layer.reposition()

    def on_stop(self):
        LOG.info("GUI closing — stopping orchestrator")
        if self._update_event:
            self._update_event.cancel()
        self.orchestrator.stop()


if __name__ == "__main__":
    # Convenience for isolated GUI smoke-testing only.
    # main.py is the real entry point — it constructs Orchestrator
    # (fusion + acquisition modules + airspace manager) before handing
    # it to this App. Running this file directly skips all of that.
    logging.basicConfig(level=logging.INFO)
    LOG.warning("Running gui/app.py directly — acquisition modules and "
                "fusion will NOT be started. Use main.py instead.")
