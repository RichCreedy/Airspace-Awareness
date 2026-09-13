"""
modules/freshness_badge.py
----------------------------
Data freshness indicator widget: a small colored badge shown in the
top bar, tap-to-expand into a detailed status popup showing last
sync times, staleness ages, and any errors per data source.

Design:
  - FreshnessBadge polls a `status_provider()` callable every
    `poll_interval_s` seconds (default 5s) via Clock.
  - status_provider() returns a dict shaped like:

      {
        "overall": "fresh" | "stale" | "error",
        "items": [
          {
            "label": str,          # e.g. "GPS Fix"
            "status": "fresh"|"stale"|"error",
            "age_s": float | None,
            "last_sync_iso": str | None,
            "detail": str,         # human-readable extra info
          },
          ...
        ],
        "errors": [str, ...],      # flat list of error strings, if any
      }

  - If no status_provider is supplied, a DEFAULT aggregator is used
    that best-effort pulls from `fusion`, `modules.airspace_manager`,
    and `modules.openaip_sync` if they're importable/available on the
    app instance. This is a stand-in for the future unified
    `sync_metadata.get_freshness_status()` — once that module exists,
    just pass it in as status_provider and delete DefaultAggregator.

Usage:
    badge = FreshnessBadge(
        status_provider=lambda: sync_metadata.get_freshness_status(app),
        retry_callback=lambda: app.airspace_manager.force_resync(),
    )
    top_bar.add_widget(badge)
"""

import logging
from datetime import datetime

from kivy.clock import Clock
from kivy.uix.widget import Widget
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.graphics import Color, Ellipse
from kivy.properties import StringProperty

logger = logging.getLogger("freshness_badge")

# ---------------------------------------------------------------------
# Status thresholds (seconds) — override via config if desired
# ---------------------------------------------------------------------
DEFAULT_WARN_AFTER_S = 60      # age_s beyond this -> "stale" (amber)
DEFAULT_ERROR_AFTER_S = 300    # age_s beyond this -> "error" (red)

STATUS_COLORS = {
    "fresh": (0.2, 0.75, 0.2, 1),   # green
    "stale": (0.9, 0.7, 0.1, 1),    # amber
    "error": (0.85, 0.15, 0.15, 1), # red
    "unknown": (0.5, 0.5, 0.5, 1),  # grey
}


def _classify_age(age_s, warn_after_s, error_after_s) -> str:
    if age_s is None:
        return "unknown"
    if age_s >= error_after_s:
        return "error"
    if age_s >= warn_after_s:
        return "stale"
    return "fresh"


def _worst_status(statuses):
    order = ["error", "stale", "unknown", "fresh"]
    for s in order:
        if s in statuses:
            return s
    return "unknown"


# ---------------------------------------------------------------------
# Default aggregator (stand-in until sync_metadata.py exists)
# ---------------------------------------------------------------------

class DefaultAggregator:
    """
    Best-effort freshness aggregator. Pulls whatever is available from
    the app instance's known attributes without hard-crashing if a
    given subsystem isn't wired up yet.

    NOTE: This exists ONLY because sync_metadata.py doesn't yet exist.
    Once it does, replace usage of this class with:
        sync_metadata.get_freshness_status(app)
    """

    def __init__(self, app, warn_after_s=DEFAULT_WARN_AFTER_S,
                 error_after_s=DEFAULT_ERROR_AFTER_S):
        self.app = app
        self.warn_after_s = warn_after_s
        self.error_after_s = error_after_s

    def __call__(self) -> dict:
        items = []
        errors = []

        # --- GPS freshness (from Fusion.get_gps_fix()) ---
        try:
            fusion = getattr(self.app, "fusion", None)
            gps = fusion.get_gps_fix() if fusion else None
            if gps:
                age_s = gps.get("age_s")
                status = "error" if gps.get("stale") else _classify_age(
                    age_s, self.warn_after_s, self.error_after_s
                )
                items.append({
                    "label": "GPS Fix",
                    "status": status,
                    "age_s": age_s,
                    "last_sync_iso": gps.get("last_seen_iso"),
                    "detail": f"fix_quality={gps.get('fix_quality')}",
                })
            else:
                items.append({
                    "label": "GPS Fix", "status": "unknown",
                    "age_s": None, "last_sync_iso": None,
                    "detail": "no GPS data available",
                })
        except Exception as e:
            errors.append(f"GPS check failed: {e}")
            items.append({"label": "GPS Fix", "status": "error",
                          "age_s": None, "last_sync_iso": None, "detail": str(e)})

        # --- Remote zone sync (modules/openaip_sync.py) ---
        try:
            airspace_mgr = getattr(self.app, "airspace_manager", None)
            sync_status = airspace_mgr.get_status() if airspace_mgr else None
            if sync_status:
                remote = sync_status.get("remote_sync", {})
                age_s = remote.get("age_s")
                status = "error" if remote.get("last_error") else _classify_age(
                    age_s, self.warn_after_s, self.error_after_s
                )
                items.append({
                    "label": "Remote Zones (OpenAIP/NOTAM)",
                    "status": status,
                    "age_s": age_s,
                    "last_sync_iso": remote.get("last_sync_iso"),
                    "detail": remote.get("last_error") or f"{remote.get('zone_count', '?')} zones",
                })
                if remote.get("last_error"):
                    errors.append(f"Remote sync: {remote['last_error']}")

                # --- FRZ regeneration ---
                frz = sync_status.get("frz", {})
                items.append({
                    "label": "FRZ Regeneration",
                    "status": "fresh" if frz.get("up_to_date") else "stale",
                    "age_s": frz.get("age_s"),
                    "last_sync_iso": frz.get("last_regen_iso"),
                    "detail": "up to date" if frz.get("up_to_date") else "source changed, pending regen",
                })
            else:
                items.append({
                    "label": "Remote Zones (OpenAIP/NOTAM)", "status": "unknown",
                    "age_s": None, "last_sync_iso": None,
                    "detail": "airspace_manager not available",
                })
        except Exception as e:
            errors.append(f"Airspace manager check failed: {e}")
            items.append({"label": "Remote Zones (OpenAIP/NOTAM)", "status": "error",
                          "age_s": None, "last_sync_iso": None, "detail": str(e)})

        # --- ADS-B (tar1090) freshness, if fusion tracks it ---
        try:
            fusion = getattr(self.app, "fusion", None)
            adsb_age = fusion.get_adsb_age_s() if fusion and hasattr(fusion, "get_adsb_age_s") else None
            if adsb_age is not None:
                status = _classify_age(adsb_age, self.warn_after_s, self.error_after_s)
                items.append({
                    "label": "ADS-B (tar1090)",
                    "status": status,
                    "age_s": adsb_age,
                    "last_sync_iso": None,
                    "detail": "",
                })
        except Exception as e:
            errors.append(f"ADS-B check failed: {e}")

        overall = _worst_status([i["status"] for i in items])
        return {"overall": overall, "items": items, "errors": errors}


# ---------------------------------------------------------------------
# Badge widget
# ---------------------------------------------------------------------

class FreshnessBadge(BoxLayout):
    """
    Small tappable badge: colored dot + short label, e.g. "🟢 Fresh".
    Tapping opens FreshnessDetailPopup with the full breakdown.
    """

    overall_status = StringProperty("unknown")

    def __init__(self, status_provider=None, retry_callback=None,
                 poll_interval_s=5.0, app=None, **kwargs):
        super().__init__(orientation="horizontal", size_hint=(None, 1),
                          width=110, spacing=4, **kwargs)

        self._retry_callback = retry_callback
        self._last_status = {"overall": "unknown", "items": [], "errors": []}

        if status_provider is not None:
            self._status_provider = status_provider
        elif app is not None:
            self._status_provider = DefaultAggregator(app)
        else:
            logger.warning("[freshness_badge] no status_provider or app given, badge will show 'unknown'")
            self._status_provider = lambda: {"overall": "unknown", "items": [], "errors": []}

        self._dot = Widget(size_hint=(None, None), size=(20, 20))
        with self._dot.canvas:
            self._dot_color = Color(*STATUS_COLORS["unknown"])
            self._dot_shape = Ellipse(pos=self._dot.pos, size=self._dot.size)
        self._dot.bind(pos=self._sync_dot_shape, size=self._sync_dot_shape)

        self._label = Label(text="—", size_hint=(None, 1), width=70, font_size=13)

        self.add_widget(self._dot)
        self.add_widget(self._label)

        self.bind(on_touch_down=self._on_touch_down)

        self._refresh()  # immediate first read
        Clock.schedule_interval(lambda dt: self._refresh(), poll_interval_s)

    def _sync_dot_shape(self, *args):
        self._dot_shape.pos = self._dot.pos
        self._dot_shape.size = self._dot.size

    def _refresh(self, *args):
        try:
            status = self._status_provider()
        except Exception as e:
            logger.error(f"[freshness_badge] status_provider raised: {e}")
            status = {"overall": "error", "items": [], "errors": [str(e)]}

        self._last_status = status
        overall = status.get("overall", "unknown")
        self.overall_status = overall
        self._dot_color.rgba = STATUS_COLORS.get(overall, STATUS_COLORS["unknown"])

        label_map = {"fresh": "Fresh", "stale": "Stale", "error": "Error", "unknown": "—"}
        self._label.text = label_map.get(overall, "—")

    def _on_touch_down(self, instance, touch):
        if self.collide_point(*touch.pos):
            self._open_detail_popup()
            return True
        return False

    def _open_detail_popup(self):
        popup = FreshnessDetailPopup(
            status=self._last_status,
            retry_callback=self._retry_callback,
            refresh_callback=self._refresh,
        )
        popup.open()


# ---------------------------------------------------------------------
# Detail popup
# ---------------------------------------------------------------------

class FreshnessDetailPopup(Popup):
    def __init__(self, status: dict, retry_callback=None, refresh_callback=None, **kwargs):
        super().__init__(title="Data Freshness", size_hint=(0.9, 0.8), **kwargs)
        self._retry_callback = retry_callback
        self._refresh_callback = refresh_callback
        self._build_ui(status)

    def _build_ui(self, status: dict):
        root = BoxLayout(orientation="vertical", padding=12, spacing=8)

        scroll = ScrollView(size_hint=(1, 0.8))
        items_box = BoxLayout(orientation="vertical", size_hint_y=None, spacing=6, padding=4)
        items_box.bind(minimum_height=items_box.setter("height"))

        for item in status.get("items", []):
            row = self._build_item_row(item)
            items_box.add_widget(row)

        if status.get("errors"):
            err_label = Label(
                text="Errors:\n" + "\n".join(f"• {e}" for e in status["errors"]),
                size_hint_y=None, color=(0.9, 0.3, 0.3, 1), halign="left",
            )
            err_label.bind(texture_size=lambda inst, val: setattr(err_label, "height", val[1]))
            items_box.add_widget(err_label)

        scroll.add_widget(items_box)
        root.add_widget(scroll)

        btn_row = BoxLayout(orientation="horizontal", size_hint=(1, 0.15), spacing=8)
        if self._retry_callback:
            retry_btn = Button(text="Retry Sync Now")
            retry_btn.bind(on_release=self._on_retry)
            btn_row.add_widget(retry_btn)
        close_btn = Button(text="Close")
        close_btn.bind(on_release=lambda *a: self.dismiss())
        btn_row.add_widget(close_btn)
        root.add_widget(btn_row)

        self.content = root

    def _build_item_row(self, item: dict) -> BoxLayout:
        row = BoxLayout(orientation="vertical", size_hint_y=None, height=70,
                         padding=4, spacing=2)

        status = item.get("status", "unknown")
        color = STATUS_COLORS.get(status, STATUS_COLORS["unknown"])

        header = BoxLayout(orientation="horizontal", size_hint_y=None, height=24)
        dot = Widget(size_hint=(None, None), size=(14, 14))
        with dot.canvas:
            Color(*color)
            Ellipse(pos=dot.pos, size=dot.size)
        header.add_widget(dot)
        header.add_widget(Label(text=item.get("label", "?"), halign="left", bold=True))
        row.add_widget(header)

        age_s = item.get("age_s")
        age_str = f"{age_s:.0f}s ago" if isinstance(age_s, (int, float)) else "n/a"
        last_sync = item.get("last_sync_iso") or "n/a"
        detail = item.get("detail", "")

        info = Label(
            text=f"Age: {age_str}   |   Last sync: {last_sync}\n{detail}",
            halign="left", font_size=12, size_hint_y=None, height=40,
        )
        row.add_widget(info)

        return row

    def _on_retry(self, *args):
        if self._retry_callback:
            try:
                self._retry_callback()
                logger.info("[freshness_badge] manual retry triggered")
            except Exception as e:
                logger.error(f"[freshness_badge] retry_callback failed: {e}")
        if self._refresh_callback:
            self._refresh_callback()
        self.dismiss()
