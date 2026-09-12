"""
Kivy App entrypoint — ties together splash, main map screen, settings.
"""
from kivy.app import App
from kivy.uix.screenmanager import ScreenManager, Screen
from kivy.uix.boxlayout import BoxLayout
from kivy.clock import Clock

from src.gui.map_view import AirspaceMapView
from src.gui.freshness_badge import FreshnessBadge
from src.gui.unverified_zone_banner import UnverifiedZoneBanner
from src.gui.settings_screen import SettingsScreen
from src.gui.disclaimer_splash import DisclaimerSplash


class MainScreen(Screen):
    def __init__(self, config, geofence_manager, fusion_engine,
                 gps_reader, sync_metadata, **kwargs):
        super().__init__(**kwargs)
        self.g
