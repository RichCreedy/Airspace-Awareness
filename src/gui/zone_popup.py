"""
Stacked, scrollable popup listing overlapping zones at a tapped point.
"""
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.button import Button


def show_zone_popup(zones):
    """zones: list of zone dicts from GeofenceManager.zones_at_point()"""
    outer = BoxLayout(orientation="vertical", padding=10, spacing=5)

    scroll = ScrollView(size_hint=(1, 1))
    inner = BoxLayout(orientation="vertical", size_hint_y=None, spacing=8)
    inner.bind(minimum_height=inner.setter("height"))

    if not zones:
        inner.add_widget(Label(text="No zones at this point.", size_hint_y=None, height=30))
    else:
        for zone in zones:
            verified = zone.get("verified", False)
            badge = "✅ verified" if verified else "⚠️ Approximate FRZ — verified: false"
            row = BoxLayout(orientation="vertical", size_hint_y=None, height=70, padding=5)
            row.add_widget(Label(text=f"[b]{zone['name']}[/b]", markup=True, size_hint_y=None, height=25))
            row.add_widget(Label(text=f"Type: {zone.get('type', 'unknown')}", size_hint_y=None, height=20))
            row.add_widget(Label(text=badge, size_hint_y=None, height=20))
            inner.add_widget(row)

    scroll.add_widget(inner)
    outer.add_widget(scroll)

    close_btn = Button(text="Close", size_hint_y=None, height=40)
    outer.add_widget(close_btn)

    popup = Popup(title=f"Zones at tap point ({len(zones)})", content=outer, size_hint=(0.85, 0.7))
    close_btn.bind(on_release=popup.dismiss)
    popup.open()
    return popup
