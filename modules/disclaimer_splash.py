"""
modules/disclaimer_splash.py
------------------------------
Dismissible safety disclaimer splash screen.

Behavior:
  - Shown automatically on app startup UNLESS the user previously
    checked "Don't show this again".
  - Always re-showable on demand from Settings (force=True), regardless
    of the persisted flag — persistent dismissal only suppresses the
    AUTOMATIC startup popup, not manual access.
  - Persisted via a small standalone JSON state file so this module
    has no dependency on the shape of the main app config.

Usage:
    from modules.disclaimer_splash import show_disclaimer_if_needed

    # at startup:
    show_disclaimer_if_needed(root_widget)

    # from a Settings screen "Show Safety Disclaimer" button:
    show_disclaimer_if_needed(root_widget, force=True)
"""

import os
import json
import logging

from kivy.uix.modalview import ModalView
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.checkbox import CheckBox
from kivy.uix.scrollview import ScrollView

logger = logging.getLogger("disclaimer_splash")

STATE_DIR = "data/state"
STATE_FILE = os.path.join(STATE_DIR, "app_state.json")

DISCLAIMER_TEXT = (
    "⚠️  SAFETY & ACCURACY DISCLAIMER\n\n"
    "This tool provides best-effort situational awareness only, "
    "assembled from passive WiFi/Bluetooth Remote ID detection, "
    "ADS-B (tar1090), GPS, and community/NOTAM-derived airspace data.\n\n"
    "It is NOT a certified aviation safety device and MUST NOT be used "
    "as the sole basis for any flight, drone operation, or airspace "
    "decision.\n\n"
    "• Detected tracks may be incomplete, delayed, or entirely absent "
    "(passive sniffing cannot guarantee detection of all aircraft or "
    "drones).\n"
    "• Airspace zone boundaries — especially those marked "
    "'⚠️ Approximate FRZ — verified: false' — are approximations and "
    "may be inaccurate or outdated.\n"
    "• Runway heading data flagged as low-confidence has not been "
    "manually verified.\n\n"
    "Always consult official NOTAMs, your national aviation authority, "
    "and certified equipment before making any real-world decision.\n\n"
    "By continuing, you acknowledge this tool is provided for "
    "situational-awareness/hobby use only, with no warranty of "
    "accuracy or fitness for any purpose."
)


# ---------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------

def _load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[disclaimer_splash] failed to read state file, resetting: {e}")
        return {}


def _save_state(state: dict) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except OSError as e:
        logger.error(f"[disclaimer_splash] failed to write state file: {e}")


def is_disclaimer_suppressed() -> bool:
    """True if user previously checked 'don't show again'."""
    return bool(_load_state().get("disclaimer_ack", False))


def set_disclaimer_suppressed(value: bool) -> None:
    state = _load_state()
    state["disclaimer_ack"] = bool(value)
    _save_state(state)
    logger.info(f"[disclaimer_splash] disclaimer_ack set to {value}")


# ---------------------------------------------------------------------
# Splash widget
# ---------------------------------------------------------------------

class DisclaimerSplash(ModalView):
    """
    Full-screen-ish modal with disclaimer text, a 'don't show again'
    checkbox, and a Continue button. Cannot be dismissed by tapping
    outside (auto_dismiss=False) — must be explicitly acknowledged,
    since this is a safety notice, not a casual dialog.
    """

    def __init__(self, **kwargs):
        super().__init__(
            size_hint=(0.9, 0.85),
            auto_dismiss=False,
            **kwargs
        )
        self._build_ui()

    def _build_ui(self):
        root = BoxLayout(orientation="vertical", padding=16, spacing=12)

        # Scrollable disclaimer text (uConsole screen is small)
        scroll = ScrollView(size_hint=(1, 0.75))
        label = Label(
            text=DISCLAIMER_TEXT,
            size_hint_y=None,
            text_size=(None, None),
            halign="left",
            valign="top",
        )
        # Wrap text to scrollview width once laid out
        def _update_text_width(instance, value):
            label.text_size = (scroll.width - 16, None)
        scroll.bind(width=_update_text_width)
        label.bind(texture_size=lambda inst, val: setattr(label, "height", val[1]))
        scroll.add_widget(label)
        root.add_widget(scroll)

        # Checkbox row
        checkbox_row = BoxLayout(orientation="horizontal", size_hint=(1, 0.1), spacing=8)
        self._checkbox = CheckBox(size_hint=(None, None), size=(32, 32))
        checkbox_row.add_widget(self._checkbox)
        checkbox_row.add_widget(Label(text="Don't show this again", halign="left"))
        root.add_widget(checkbox_row)

        # Continue button
        continue_btn = Button(
            text="I Understand — Continue",
            size_hint=(1, 0.15),
            background_color=(0.2, 0.5, 0.2, 1),
        )
        continue_btn.bind(on_release=self._on_continue)
        root.add_widget(continue_btn)

        self.add_widget(root)

    def _on_continue(self, *args):
        if self._checkbox.active:
            set_disclaimer_suppressed(True)
        self.dismiss()


# ---------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------

def show_disclaimer_if_needed(parent_widget=None, force: bool = False) -> bool:
    """
    Show the disclaimer splash.

    Args:
        parent_widget: unused currently (ModalView attaches to the
            top-level window automatically) — kept for API symmetry /
            future use (e.g. logging which screen triggered it).
        force: if True, always show regardless of persisted
            "don't show again" flag. Use this for the Settings
            screen's "Show Safety Disclaimer" button.

    Returns:
        True if the splash was actually shown, False if suppressed.
    """
    if not force and is_disclaimer_suppressed():
        logger.info("[disclaimer_splash] suppressed by persisted user preference, not showing")
        return False

    splash = DisclaimerSplash()
    splash.open()
    logger.info(f"[disclaimer_splash] shown (force={force})")
    return True
