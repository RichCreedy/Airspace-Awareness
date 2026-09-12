"""
Proximity alert banner — warns of unverified zones within a
configurable radius. Tap to view details. Updates every 15s.
"""
from kivy.uix.button import Button
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.popup import Popup
from kivy.uix.label import Label
from kivy.clock import Clock

from src.airspace.proximity_alert import check_unverified_zones_nearby


class UnverifiedZoneBanner(BoxLayout):
    def __init__(self, get_gps_fix, get_zones, radius_km=5, check_interval_sec=15, **kwargs):
        super().__init__(orientation="horizontal", size_hint_y=None, height=0, **kwargs)
        self.get_gps_fix = get_gps_fix
        self.get_zones = get_zones
        self.radius_km = radius_km
        self._nearby = []
        self._dismissed = False

        self.label_btn = Button(text="", background_color=(1, 0.6, 0, 1))
        self.label_btn.bind(on_release=self._show_details)
        self.add_widget(self.label_btn)

        Clock.schedule_interval(self._refresh, check_interval_sec)

    def _refresh(self, *args):
        if self._dismissed:
            return
        fix = self.get_gps_fix()
        if not fix or fix.get("lat") is None:
            self._hide()
            return

        zones = self.get_zones()
        self._nearby = check_unverified_zones_nearby(
            fix["lat"], fix["lon"], zones, self.radius_km
        )

        if self._nearby:
            self.label_btn.text = (
                f"⚠️ {len(self._nearby)} unverified zone(s) within {self.radius_km}km — tap for details"
            )
            self.height = 40
        else:
            self._hide()

    def _hide(self):
        self.height = 0
        self.label_btn.text = ""

    def _show_details(self, *args):
        content = BoxLayout(orientation="vertical", padding=10, spacing=5)
        for zone, dist in self._nearby:
            content.add_widget(
                Label(text=f"{zone['name']} — {dist:.1f} km — verified: false")
            )
        close_btn = Button(text="Close", size_hint_y=None, height=40)
        popup = Popup(title="Unverified Zones Nearby", content=content, size_hint=(0.8, 0.6))
        close_btn.bind(on_release=popup.dismiss)
        content.add_widget(close_btn)
        popup.open()
