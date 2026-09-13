"""
Settings screen — re-show disclaimer, adjust refresh interval,
proximity alert config.
"""
from kivy.uix.screenmanager import Screen
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.slider import Slider

from modules.disclaimer_splash import show_disclaimer_if_needed

Button(
    text="Show Safety Disclaimer",
    on_release=lambda *a: show_disclaimer_if_needed(force=True),
)



class SettingsScreen(Screen):
    def __init__(self, config, **kwargs):
        super().__init__(**kwargs)
        self.config = config

        layout = BoxLayout(orientation="vertical", padding=20, spacing=15)

        show_disclaimer_btn = Button(text="Show Safety Disclaimer Again", size_hint_y=None, height=50)
        show_disclaimer_btn.bind(on_release=self._show_disclaimer)
        layout.add_widget(show_disclaimer_btn)

        layout.add_widget(Label(text="Proximity Alert Radius (km)"))
        self.radius_slider = Slider(
            min=1, max=20,
            value=self.config["airspace"]["proximity_alert"]["radius_km"],
        )
        self.radius_slider.bind(value=self._on_radius_change)
        layout.add_widget(self.radius_slider)

        self.add_widget(layout)

    def _show_disclaimer(self, *args):
        splash = DisclaimerSplash(
            flag_file=self.config["disclaimer"]["flag_file"]
        )
        splash.open()

    def _on_radius_change(self, instance, value):
        self.config["airspace"]["proximity_alert"]["radius_km"] = value
        # TODO: persist back to config.yaml on change or on app exit
