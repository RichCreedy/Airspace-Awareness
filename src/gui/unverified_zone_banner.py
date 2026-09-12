"""
modules/unverified_zone_banner.py — UI widget for the unverified-zone
proximity banner.

Resolves the open question: "banner or counter for unverified zones near
GPS position." This module owns ONLY the Kivy widget / display behaviour;
the actual nearest-zone detection math lives in modules/proximity_alert.py
so this stays swappable and unit-testable without pulling in Kivy.

⚠️ ASSUMPTION: modules/proximity_alert.py exposes a function with this
shape (adjust the import + call below if the real signature differs):

    find_nearby_unverified_zones(
        zones: list[dict],
        lat: float,
        lon: float,
        radius_m: float,
    ) -> list[dict]

⚠️ ASSUMPTION: config.yaml's `airspace.proximity_alert` block looks like:

    airspace:
      proximity_alert:
        enabled: true
        radius_m: 5000
        check_interval_s: 15

Both flagged for reconciliation if the real module/config differ.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from kivy.clock import Clock
from kivy.graphics import Color, Rectangle
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label

from modules.proximity_alert import find_nearby_unverified_zones

LOG = logging.getLogger("modules.unverified_zone_banner")

DEFAULT_RADIUS_M = 5000
DEFAULT_INTERVAL_S = 15
SNOOZE_S = 300


class UnverifiedZoneBanner(BoxLayout):
    """
    Dismissible/snoozable banner shown above the map when one or more
    unverified geofence zones are within `radius_m` of the current GPS
    fix. Polled on a Kivy Clock interval (default 15s) rather than
    pushed from the background ProximityAlertMonitor thread, to keep
    all widget mutation on the GUI thread.

    Construction is deliberately decoupled from gui/app.py's popup
    helpers via the `on_view_pressed` callback, to avoid a circular
    import between this module and gui.app.
    """

    def __init__(
        self,
        orchestrator,
        on_view_pressed: Optional[Callable[[list], None]] = None,
        **kwargs,
    ):
        super().__init__(
            orientation="horizontal",
            size_hint_y=None,
            height=0,
            spacing=dp(8),
            padding=(dp(10), dp(4)),
            **kwargs,
        )
        self.orchestrator = orchestrator
        self._on_view_pressed = on_view_pressed

        cfg = self._get_proximity_config()
        self.enabled = cfg.get("enabled", True)
        self.radius_m = cfg.get("radius_m", DEFAULT_RADIUS_M)
        self.interval_s = cfg.get("check_interval_s", DEFAULT_INTERVAL_S)

        self._snoozed_until = 0.0
        self._current_zones: list = []
        self._visible = False

        self.bind(pos=self._redraw_bg, size=self._redraw_bg)

        self._label = Label(text="", halign="left", valign="middle", shorten=True)
        self._label.bind(size=lambda inst, sz: setattr(inst, "text_size", sz))

        self._view_btn = Button(text="View", size_hint=(None, 1), width=dp(70))
        self._dismiss_btn = Button(text="✕", size_hint=(None, 1), width=dp(44))
        self._view_btn.bind(on_release=self._on_view_button_pressed)
        self._dismiss_btn.bind(on_release=self._on_dismiss_pressed)

        self.opacity = 0

    # ------------------------------------------------------------------
    def _get_proximity_config(self) -> dict:
        try:
            return self.orchestrator.config.get("airspace", {}).get("proximity_alert", {})
        except Exception:
            LOG.exception("Failed to read airspace.proximity_alert config")
            return {}

    def _redraw_bg(self, *_args):
        self.canvas.before.clear()
        with self.canvas.before:
            Color(0.55, 0.35, 0.0, 0.9)
            Rectangle(pos=self.pos, size=self.size)

    # ------------------------------------------------------------------
    def start(self):
        """Call once after the widget is on-screen. Schedules polling."""
        if not self.enabled:
            LOG.info("Proximity alert banner disabled via config")
            return
        self.refresh()
        Clock.schedule_interval(lambda dt: self.refresh(), self.interval_s)

    def refresh(self, *_args):
        if not self.enabled:
            return
        if time.time() < self._snoozed_until:
            return

        zones = self._compute_nearby_zones()
        self._current_zones = zones

        if not zones:
            self._hide()
            return

        self._show(zones)

    def _compute_nearby_zones(self) -> list:
        gps_fix = self._get_gps_fix()
        if not gps_fix or not gps_fix.get("valid", False):
            return []

        lat, lon = gps_fix.get("lat"), gps_fix.get("lon")
        if lat is None or lon is None:
            return []

        zones = self._get_zones()
        if not zones:
            return []

        try:
            return find_nearby_unverified_zones(zones, lat, lon, self.radius_m)
        except Exception:
            LOG.exception("proximity_alert.find_nearby_unverified_zones() failed")
            return []

    def _get_gps_fix(self) -> Optional[dict]:
        try:
            return self.orchestrator.fusion.get_snapshot().get("gps_fix")
        except Exception:
            LOG.exception("Failed to get GPS fix for proximity banner")
            return None

    def _get_zones(self) -> list:
        """
        ⚠️ ASSUMPTION: prefers orchestrator.airspace_manager.get_zones()
        (the authoritative live merged set per the AirspaceManager design)
        and falls back to fusion.get_snapshot()["zones"] if that accessor
        isn't available yet.
        """
        try:
            return self.orchestrator.airspace_manager.get_zones()
        except AttributeError:
            pass
        except Exception:
            LOG.exception("airspace_manager.get_zones() failed")

        try:
            return self.orchestrator.fusion.get_snapshot().get("zones", []) or []
        except Exception:
            LOG.exception("Failed to get zones from fusion snapshot")
            return []

    # ------------------------------------------------------------------
    def _show(self, zones: list):
        count = len(zones)
        self._label.text = f"⚠️ {count} unverified zone{'s' if count != 1 else ''} nearby"

        if not self._visible:
            self.clear_widgets()
            self.add_widget(self._label)
            self.add_widget(self._view_btn)
            self.add_widget(self._dismiss_btn)
            self._visible = True

        self.height = dp(40)
        self.opacity = 1

    def _hide(self):
        self.height = 0
        self.opacity = 0
        self.clear_widgets()
        self._visible = False

    def _on_view_button_pressed(self, *_args):
        if not self._current_zones:
            return
        if self._on_view_pressed:
            self._on_view_pressed(self._current_zones)
        else:
            LOG.warning("UnverifiedZoneBanner has no on_view_pressed callback wired")

    def _on_dismiss_pressed(self, *_args):
        self._snoozed_until = time.time() + SNOOZE_S
        self._hide()
