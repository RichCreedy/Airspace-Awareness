"""
unverified_zone_banner.py
--------------------------
Banner widget warning of unverified airspace zones near the current
GPS position. Polls Fusion's snapshot every `check_interval_s`
(default 15s) via proximity_alert.check_unverified_zones_nearby().

Config (config.yaml):
    airspace:
      proximity_alert:
        enabled: true
        radius_m: 5000
        check_interval_s: 15
        snooze_duration_s: 300   # ⚠️ ASSUMPTION - confirm default
"""

import time
import logging
from typing import Optional, Callable, List, Dict, Any

from kivy.clock import Clock
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.gridlayout import GridLayout
from kivy.properties import BooleanProperty

from proximity_alert import check_unverified_zones_nearby

logger = logging.getLogger(__name__)


class UnverifiedZoneBanner(BoxLayout):
    """
    A dismissible/snoozable banner shown above the map, below the top bar,
    warning of unverified zones within a configurable radius of the
    current GPS position.
    """

    visible = BooleanProperty(False)

    def __init__(
        self,
        fusion,
        config: Optional[Dict[str, Any]] = None,
        on_tap_details: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
        **kwargs,
    ):
        """
        Parameters
        ----------
        fusion : Fusion
            The shared Fusion instance (must expose get_snapshot()).
        config : dict
            The `airspace.proximity_alert` sub-section of config.yaml.
        on_tap_details : callable, optional
            Override for what happens when the banner is tapped.
            Defaults to opening an internal Popup listing zones.
        """
        super().__init__(orientation="horizontal", size_hint_y=None, height=0, **kwargs)

        self._fusion = fusion
        cfg = config or {}
        self._enabled: bool = cfg.get("enabled", True)
        self._radius_m: float = cfg.get("radius_m", 5000.0)
        self._check_interval_s: float = cfg.get("check_interval_s", 15.0)
        self._snooze_duration_s: float = cfg.get("snooze_duration_s", 300.0)

        self._on_tap_details = on_tap_details or self._default_show_details

        self._snoozed_until: float = 0.0
        self._dismissed_zone_ids: set = set()
        self._current_matches: List[Dict[str, Any]] = []

        self._label = Label(
            text="",
            halign="left",
            valign="middle",
            shorten=True,
            size_hint_x=0.75,
        )
        self._label.bind(size=self._label.setter("text_size"))

        self._dismiss_btn = Button(text="✕", size_hint_x=0.1)
        self._dismiss_btn.bind(on_release=lambda *_: self._dismiss())

        self._snooze_btn = Button(text="Snooze", size_hint_x=0.15)
        self._snooze_btn.bind(on_release=lambda *_: self._snooze())

        self.add_widget(self._label)
        self.add_widget(self._snooze_btn)
        self.add_widget(self._dismiss_btn)

        # Tapping the label (not the buttons) shows details
        self._label.bind(on_touch_down=self._on_label_touch)

        self._refresh_event = None
        if self._enabled:
            self.start()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self):
        """Begin periodic polling."""
        if self._refresh_event is not None:
            return
        self._refresh_event = Clock.schedule_interval(
            lambda dt: self._refresh(), self._check_interval_s
        )
        self._refresh()  # immediate first check

    def stop(self):
        """Stop periodic polling and hide the banner."""
        if self._refresh_event is not None:
            self._refresh_event.cancel()
            self._refresh_event = None
        self._set_visible(False)

    # ------------------------------------------------------------------
    # Core refresh logic
    # ------------------------------------------------------------------
    def _refresh(self):
        if not self._enabled:
            self._set_visible(False)
            return

        now = time.monotonic()
        if now < self._snoozed_until:
            self._set_visible(False)
            return

        try:
            snapshot = self._fusion.get_snapshot()
        except Exception:
            logger.exception("UnverifiedZoneBanner: failed to get snapshot")
            self._set_visible(False)
            return

        gps_fix = snapshot.get("gps")
        zones = snapshot.get("zones", [])

        matches = check_unverified_zones_nearby(
            gps_fix, zones, radius_m=self._radius_m
        )

        # Filter out zones the user has explicitly dismissed, UNLESS
        # a *new* unverified zone (not previously dismissed) has
        # entered range — in which case we re-show the banner.
        new_ids = {m["zone_id"] for m in matches}
        undismissed = [m for m in matches if m["zone_id"] not in self._dismissed_zone_ids]

        self._current_matches = matches

        if not matches:
            self._set_visible(False)
            # Clear stale dismissals once nothing is nearby, so a
            # future re-entry into the same zone alerts again.
            self._dismissed_zone_ids.clear()
            return

        if not undismissed:
            # Everything currently nearby was already dismissed.
            self._set_visible(False)
            return

        count = len(undismissed)
        nearest = undismissed[0]
        if count == 1:
            text = (
                f"⚠️ Unverified zone nearby: {nearest['name']} "
                f"(~{int(nearest['distance_m'])} m)"
            )
        else:
            text = (
                f"⚠️ {count} unverified zones nearby — nearest: "
                f"{nearest['name']} (~{int(nearest['distance_m'])} m)"
            )
        self._label.text = text
        self._set_visible(True)

    def _set_visible(self, visible: bool):
        self.visible = visible
        self.height = 40 if visible else 0
        self.opacity = 1 if visible else 0
        self.disabled = not visible

    # ------------------------------------------------------------------
    # User interactions
    # ------------------------------------------------------------------
    def _dismiss(self):
        """Hide banner for currently-matched zones until they clear
        and a new one appears (or the zone set changes)."""
        for m in self._current_matches:
            self._dismissed_zone_ids.add(m["zone_id"])
        self._set_visible(False)

    def _snooze(self):
        """Hide banner entirely for `snooze_duration_s`, regardless
        of zone changes."""
        self._snoozed_until = time.monotonic() + self._snooze_duration_s
        self._set_visible(False)

    def _on_label_touch(self, instance, touch):
        if instance.collide_point(*touch.pos) and self.visible:
            self._on_tap_details(self._current_matches)
            return True
        return False

    # ------------------------------------------------------------------
    # Default details popup
    # ------------------------------------------------------------------
    def _default_show_details(self, matches: List[Dict[str, Any]]):
        layout = GridLayout(cols=1, size_hint_y=None, spacing=4, padding=8)
        layout.bind(minimum_height=layout.setter("height"))

        for m in matches:
            row = Label(
                text=f"{m['name']}  —  ~{int(m['distance_m'])} m  [unverified]",
                size_hint_y=None,
                height=30,
                halign="left",
                valign="middle",
            )
            row.bind(size=row.setter("text_size"))
            layout.add_widget(row)

        scroll = ScrollView(size_hint=(1, 1))
        scroll.add_widget(layout)

        popup = Popup(
            title=f"⚠️ Unverified Zones ({len(matches)})",
            content=scroll,
            size_hint=(0.85, 0.6),
        )
        popup.open()

    # ------------------------------------------------------------------
    # Config hot-reload support
    # ------------------------------------------------------------------
    def update_config(self, config: Dict[str, Any]):
        """Call this if config.yaml is reloaded at runtime."""
        was_enabled = self._enabled
        self._enabled = config.get("enabled", True)
        self._radius_m = config.get("radius_m", self._radius_m)
        new_interval = config.get("check_interval_s", self._check_interval_s)
        self._snooze_duration_s = config.get("snooze_duration_s", self._snooze_duration_s)

        if new_interval != self._check_interval_s:
            self._check_interval_s = new_interval
            if self._refresh_event is not None:
                self._refresh_event.cancel()
                self._refresh_event = None

        if self._enabled and not was_enabled:
            self.start()
        elif not self._enabled and was_enabled:
            self.stop()
