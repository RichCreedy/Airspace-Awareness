"""
unverified_zone_banner.py
==========================

UI widget: banner shown above the map (below the top bar) warning the
pilot that one or more UNVERIFIED airspace zones (e.g. auto-generated
FRZ polygons not yet human-checked) are within a configurable radius
of the current GPS position.

Behaviour
---------
* Polls every `check_interval_s` (default 15s) via Kivy's Clock.
* Uses `orchestrator.fusion.get_snapshot()` for both GPS fix and zones
  — never talks to GPS/zone sources directly (single source of truth).
* Hides itself automatically when:
    - proximity_alert is disabled in config, OR
    - no GPS fix is available / fix is stale, OR
    - zero unverified zones are within radius, OR
    - the user has dismissed/snoozed it.
* Tap the banner -> popup listing each nearby unverified zone with its
  name and distance, sorted nearest-first.
* "X" button dismisses until the next re-trigger (new zone enters
  radius). Long-press / snooze button suppresses re-triggering for
  `snooze_minutes` even if zones remain nearby.

Known limitations (carried over from proximity_alert.py)
----------------------------------------------------------
* Distance is nearest-vertex approximation, NOT true point-to-polygon
  edge distance. A zone edge could be closer than reported.
* Assumes `get_snapshot()["gps"]` is exactly `Fusion.get_gps_fix()`'s
  return shape (soft assumption, not yet verified byte-for-byte).
"""

from __future__ import annotations

import logging
import time
from typing import Optional, List, Dict, Any

from kivy.clock import Clock
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.gridlayout import GridLayout

from proximity_alert import find_nearby_unverified_zones

LOG = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# Defaults (overridden by config.yaml -> airspace.proximity_alert.*)
# ----------------------------------------------------------------------
DEFAULT_ENABLED = True
DEFAULT_RADIUS_M = 5000.0          # 5 km
DEFAULT_CHECK_INTERVAL_S = 15.0
DEFAULT_SNOOZE_MINUTES = 10.0


class UnverifiedZoneBanner(BoxLayout):
    """
    A slim horizontal warning banner. Instantiate once, add to your
    root layout (e.g. between the top bar and the MapView), then call
    `start()` once the orchestrator is running.
    """

    def __init__(self, orchestrator, config: Optional[dict] = None, **kwargs):
        kwargs.setdefault("orientation", "horizontal")
        kwargs.setdefault("size_hint_y", None)
        kwargs.setdefault("height", 0)          # hidden until triggered
        kwargs.setdefault("padding", (dp(8), dp(4)))
        kwargs.setdefault("spacing", dp(8))
        super().__init__(**kwargs)

        self.orchestrator = orchestrator

        cfg = (config or {}).get("airspace", {}).get("proximity_alert", {}) \
            if config else {}
        self.enabled: bool = cfg.get("enabled", DEFAULT_ENABLED)
        self.radius_m: float = float(cfg.get("radius_m", DEFAULT_RADIUS_M))
        self.check_interval_s: float = float(
            cfg.get("check_interval_s", DEFAULT_CHECK_INTERVAL_S)
        )
        self.snooze_minutes: float = float(
            cfg.get("snooze_minutes", DEFAULT_SNOOZE_MINUTES)
        )

        self._nearby_zones: List[Dict[str, Any]] = []
        self._dismissed: bool = False
        self._snoozed_until: float = 0.0  # monotonic timestamp
        self._clock_event = None

        self._build_ui()
        self._set_visible(False)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        with self.canvas.before:
            from kivy.graphics import Color, Rectangle
            self._bg_color = Color(0.85, 0.55, 0.0, 0.95)  # amber warning
            self._bg_rect = Rectangle(pos=self.pos, size=self.size)
        self.bind(pos=self._update_bg, size=self._update_bg)

        self.label = Label(
            text="",
            markup=True,
            halign="left",
            valign="middle",
            size_hint_x=1,
        )
        self.label.bind(size=lambda inst, val: setattr(inst, "text_size", val))
        self.label.bind(on_touch_down=self._on_label_touch)
        self.add_widget(self.label)

        self.snooze_btn = Button(
            text=f"Snooze {int(self.snooze_minutes)}m",
            size_hint_x=None,
            width=dp(90),
        )
        self.snooze_btn.bind(on_release=lambda *_: self.snooze())
        self.add_widget(self.snooze_btn)

        self.close_btn = Button(
            text="X",
            size_hint_x=None,
            width=dp(36),
        )
        self.close_btn.bind(on_release=lambda *_: self.dismiss())
        self.add_widget(self.close_btn)

    def _update_bg(self, *_):
        self._bg_rect.pos = self.pos
        self._bg_rect.size = self.size

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self):
        """Begin periodic polling. Safe to call multiple times."""
        if not self.enabled:
            LOG.info("UnverifiedZoneBanner disabled via config; not starting.")
            return
        if self._clock_event is not None:
            return
        self._clock_event = Clock.schedule_interval(
            self._on_tick, self.check_interval_s
        )
        # Run one check immediately rather than waiting for first interval
        Clock.schedule_once(lambda dt: self._on_tick(0), 0)
        LOG.info(
            "UnverifiedZoneBanner started (radius=%.0fm, interval=%.0fs)",
            self.radius_m, self.check_interval_s,
        )

    def stop(self):
        if self._clock_event is not None:
            self._clock_event.cancel()
            self._clock_event = None

    # ------------------------------------------------------------------
    # Data access (single source of truth: fusion snapshot)
    # ------------------------------------------------------------------
    def _get_gps_fix(self) -> Optional[dict]:
        """
        Mirrors Fusion.get_gps_fix()'s return shape:
            {lat, lon, altitude_m, heading, speed_mps, fix_quality,
             last_seen, last_seen_iso, age_s, stale}
        or None if no fix has ever been received.

        NOTE: there is NO "valid" key. Liveness = `not fix["stale"]`.
        """
        try:
            snapshot = self.orchestrator.fusion.get_snapshot()
        except Exception:
            LOG.exception("Failed to get fusion snapshot for GPS fix")
            return None
        return snapshot.get("gps")

    def _get_zones(self) -> List[dict]:
        try:
            snapshot = self.orchestrator.fusion.get_snapshot()
        except Exception:
            LOG.exception("Failed to get fusion snapshot for zones")
            return []
        return snapshot.get("zones", []) or []

    def _compute_nearby_zones(self) -> List[dict]:
        gps_fix = self._get_gps_fix()
        if not gps_fix:
            return []
        if gps_fix.get("stale", True):
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

    # ------------------------------------------------------------------
    # Periodic tick
    # ------------------------------------------------------------------
    def _on_tick(self, _dt):
        if not self.enabled:
            self._set_visible(False)
            return

        now = time.monotonic()
        if now < self._snoozed_until:
            self._set_visible(False)
            return

        nearby = self._compute_nearby_zones()

        # If a NEW zone has appeared since dismissal, un-dismiss.
        if self._dismissed:
            prev_ids = {z.get("id") or z.get("name") for z in self._nearby_zones}
            new_ids = {z.get("id") or z.get("name") for z in nearby}
            if new_ids - prev_ids:
                self._dismissed = False

        self._nearby_zones = nearby

        if not nearby or self._dismissed:
            self._set_visible(False)
            return

        self._update_label(nearby)
        self._set_visible(True)

    def _update_label(self, nearby: List[dict]):
        count = len(nearby)
        plural = "zone" if count == 1 else "zones"
        nearest = min(
            nearby,
            key=lambda z: z.get("_distance_m", z.get("_distance_km", 0) * 1000.0),
            default=None,
        )
        nearest_txt = ""
        if nearest is not None:
            dist_m = nearest.get(
                "_distance_m", nearest.get("_distance_km", 0) * 1000.0
            )
            nearest_txt = f" — nearest {dist_m:,.0f} m"
        self.label.text = (
            f"[b]⚠️ {count} unverified {plural} nearby[/b]{nearest_txt} "
            f"(tap for details)"
        )

    def _set_visible(self, visible: bool):
        target_height = dp(40) if visible else 0
        if self.height != target_height:
            self.height = target_height
        self.opacity = 1 if visible else 0
        self.disabled = not visible

    # ------------------------------------------------------------------
    # User actions
    # ------------------------------------------------------------------
    def dismiss(self):
        """Hide banner until a *new* unverified zone enters radius."""
        self._dismissed = True
        self._set_visible(False)
        LOG.debug("UnverifiedZoneBanner dismissed by user")

    def snooze(self):
        """Suppress banner entirely for `snooze_minutes`."""
        self._snoozed_until = time.monotonic() + (self.snooze_minutes * 60.0)
        self._dismissed = False
        self._set_visible(False)
        LOG.info(
            "UnverifiedZoneBanner snoozed for %.0f minutes", self.snooze_minutes
        )

    def _on_label_touch(self, instance, touch):
        if instance.collide_point(*touch.pos):
            self._show_details_popup()
            return True
        return False

    # ------------------------------------------------------------------
    # Tap-to-view details popup
    # ------------------------------------------------------------------
    def _show_details_popup(self):
        if not self._nearby_zones:
            return

        layout = GridLayout(cols=1, size_hint_y=None, spacing=dp(6), padding=dp(8))
        layout.bind(minimum_height=layout.setter("height"))

        for zone in sorted(
            self._nearby_zones,
            key=lambda z: z.get("_distance_m", z.get("_distance_km", 0) * 1000.0),
        ):
            name = zone.get("name") or zone.get("id") or "Unnamed zone"
            dist_m = zone.get(
                "_distance_m", zone.get("_distance_km", 0) * 1000.0
            )
            verified = zone.get("verified", False)
            row_text = (
                f"[b]{name}[/b]\n"
                f"Distance: {dist_m:,.0f} m\n"
                f"Verified: {'✅' if verified else '❌ (unverified)'}"
            )
            layout.add_widget(
                Label(
                    text=row_text,
                    markup=True,
                    size_hint_y=None,
                    height=dp(70),
                    halign="left",
                    valign="top",
                )
            )

        scroll = ScrollView(size_hint=(1, 1))
        scroll.add_widget(layout)

        popup = Popup(
            title=f"⚠️ {len(self._nearby_zones)} Unverified Zone(s) Nearby",
            content=scroll,
            size_hint=(0.85, 0.7),
        )
        popup.open()
