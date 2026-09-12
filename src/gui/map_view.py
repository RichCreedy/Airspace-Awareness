"""
Map widget — wraps kivy_garden.mapview, overlays aircraft/geofence/ownship
markers, handles tap-to-inspect zone popups.
"""
from kivy_garden.mapview import MapView, MapMarker
from kivy.graphics import Color, Line
from kivy.uix.label import Label

from src.gui.zone_popup import show_zone_popup


class AirspaceMapView(MapView):
    def __init__(self, geofence_manager, fusion_engine, **kwargs):
        super().__init__(**kwargs)
        self.geofence_manager = geofence_manager
        self.fusion_engine = fusion_engine
        self._track_markers = {}
        self._zone_overlays_drawn = False

    def on_touch_up(self, touch):
        if self.collide_point(*touch.pos):
            lat, lon = self.get_latlon_at(touch.x, touch.y)
            zones = self.geofence_manager.zones_at_point(lat, lon)
            if zones:
                show_zone_popup(zones)
                return True
        return super().on_touch_up(touch)

    def get_latlon_at(self, x, y):
        # kivy_garden.mapview provides get_latlon; fallback approximation if missing
        try:
            return self.get_latlon(x, y)
        except AttributeError:
            return self.lat, self.lon

    def update_ownship(self, lat, lon):
        if not hasattr(self, "_ownship_marker"):
            self._ownship_marker = MapMarker(lat=lat, lon=lon, source="images/icons/ownship.png")
            self.add_marker(self._ownship_marker)
        else:
            self._ownship_marker.lat = lat
            self._ownship_marker.lon = lon
        self.center_on(lat, lon)

    def update_tracks(self):
        tracks = self.fusion_engine.get_all_tracks()
        seen_ids = set()

        for track in tracks:
            tid = track["id"]
            seen_ids.add(tid)
            icon = self.fusion_engine.icon_for(track)

            if tid in self._track_markers:
                marker = self._track_markers[tid]
                marker.lat = track["lat"]
                marker.lon = track["lon"]
            else:
                marker = MapMarker(lat=track["lat"], lon=track["lon"], source=icon)
                self._track_markers[tid] = marker
                self.add_marker(marker)

        # Remove markers for tracks no longer present
        for tid in list(self._track_markers.keys()):
            if tid not in seen_ids:
                self.remove_marker(self._track_markers[tid])
                del self._track_markers[tid]

    def draw_geofence_overlays(self):
        """Draw zone polygons with warning labels for unverified zones."""
        for zone in self.geofence_manager.get_zones():
            # TODO: convert lat/lon polygon coords to screen coords and draw
            # with self.canvas + Line(points=...). kivy_garden.mapview has
            # MapLayer subclassing for this — implement a custom
            # GeofenceLayer(MapLayer) for production use.
            pass
