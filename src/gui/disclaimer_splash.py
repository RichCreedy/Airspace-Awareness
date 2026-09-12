"""
Dismissible safety disclaimer splash screen with persistent
"don't show again" flag.
"""
import os
from kivy.uix.modalview import ModalView
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.checkbox import CheckBox

FLAG_FILE_DEFAULT = "data/.disclaimer_dismissed"

DISCLAIMER_TEXT = (
    "⚠️ SAFETY DISCLAIMER\n\n"
    "This tool provides supplementary airspace awareness information only. "
    "It is NOT a certified aviation safety device. Zone boundaries, NOTAMs, "
    "and detection data may be inaccurate, incomplete, or unverified.\n\n"
    "Do not rely on this tool as your sole source of airspace information. "
    "Always consult official sources (NOTAMs, ATC, CAA) before flight.\n\n"
    "Contact: info@gr8computerservices.co.uk"
)


class DisclaimerSplash(ModalView):
    def __init__(self, flag_file=FLAG_FILE_DEFAULT, **kwargs):
        super().__init__(size_hint=(0.85, 0.75), auto_dismiss=False, **kwargs)
        self.flag_file = flag_file

        layout = BoxLayout(orientation="vertical", padding=20, spacing=10)
        label = Label(text=DISCLAIMER_TEXT, halign="left", valign="top")
        label.bind(size=lambda i, v: setattr(i, "text_size", v))
        layout.add_widget(label)

        checkbox_row = BoxLayout(size_hint_y=None, height=40, spacing=10)
        self.checkbox = CheckBox()
        checkbox_row.add_widget(self.checkbox)
        checkbox_row.add_widget(Label(text="Don't show this again"))
        layout.add_widget(checkbox_row)

        dismiss_btn = Button(text="I Understand — Continue", size_hint_y=None, height=50)
        dismiss_btn.bind(on_release=self._on_dismiss)
        layout.add_widget(dismiss_btn)

        self.add_widget(layout)

    def _on_dismiss(self, *args):
        if self.checkbox.active:
            os.makedirs(os.path.dirname(self.flag_file), exist_ok=True)
            with open(self.flag_file, "w") as f:
                f.write("dismissed")
        self.dismiss()

    @staticmethod
    def should_show(flag_file=FLAG_FILE_DEFAULT, force=False):
        if force:
            return True
        return not os.path.exists(flag_file)
