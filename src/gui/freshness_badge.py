"""
Data freshness indicator widget — colored badge + detailed status popup.
"""
import time
from kivy.uix.button import Button
from kivy.uix.popup import Popup
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.clock import Clock


class FreshnessBadge(Button):
    def __init__(self, sync_metadata, max_age_sec=3600, **kwargs):
        super().__init__(size_hint=(None, None), size=(120, 40), **kwargs)
        self.sync_metadata = sync_metadata
        self.max_age_sec = max_age_sec
        self.bind(on_release=self.show_popup)
        Clock.schedule_interval(self._refresh, 10)
        self._refresh()

    def _refresh(self, *args):
        statuses = self.sync_metadata.get_all()
        stale = any(
            self.sync_metadata.is_stale(src, self.max_age_sec)
            for src in statuses
        ) or not statuses

        if stale:
            self.text = "🔴 Stale"
            self.background_color = (0.8, 0.2, 0.2, 1)
        else:
            self.text = "🟢 Fresh"
            self.background_color = (0.2, 0.7, 0.2, 1)

    def show_popup(self, *args):
        content = BoxLayout(orientation="vertical", padding=10, spacing=5)
        statuses = self.sync_metadata.get_all()

        if not statuses:
            content.add_widget(Label(text="No sync data available yet."))
        else:
            for source, info in statuses.items():
                last = info.get("last_success")
                last_str = time.strftime("%H:%M:%S", time.localtime(last)) if last else "Never"
                err = info.get("last_error")
                err_str = f"⚠️ {err['message']}" if err else "OK"
                content.add_widget(Label(text=f"{source}: last={last_str} | {err_str}"))

        close_btn = Button(text="Close", size_hint_y=None, height=40)
        popup = Popup(title="Data Freshness Status", content=content, size_hint=(0.8, 0.6))
        close_btn.bind(on_release=popup.dismiss)
        content.add_widget(close_btn)
        popup.open()
