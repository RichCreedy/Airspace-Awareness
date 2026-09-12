#!/usr/bin/env python3
"""
main.py — Airspace Awareness Tool Orchestrator

Wires together:
  - GPS reader (gpsd via /dev/ttyAMA0)
  - Passive WiFi Remote ID capture (scapy, monitor mode)
  - Bluetooth Remote ID capture (Bleak, async)
  - ADS-B ingest (tar1090 aircraft.json)
  - Fusion layer (unified track store)
  - Airspace manager (NOTAM/NFZ sync, FRZ regen, geofences)
  - Proximity alert monitor (unverified zone warnings)
  - Kivy GUI (map, overlays, splash, freshness badge, banners)

Threading model:
  - Kivy owns the main thread (required by the framework)
  - GPS, WiFi, ADS-B run in daemon threads, each writing into
    a thread-safe FusionEngine instance
  - Bluetooth (Bleak) runs its own asyncio loop in a dedicated thread,
    since Bleak is async-only
  - AirspaceManager runs a background scheduler thread for
    auto-refresh (with backoff) and mtime-triggered FRZ regen
  - ProximityAlertMonitor runs on a lightweight periodic thread,
    polling GPS + zone data every N seconds (configurable)

Shutdown:
  - SIGINT/SIGTERM triggers a coordinated stop() on all workers
  - Kivy's on_stop() hook is wired to the same shutdown path
"""

import sys
import signal
import logging
import threading
import argparse
from pathlib import Path

# --- Project modules ------------------------------------------------------
try:
    from modules.config_loader import load_config
    from modules.gps_reader import GPSReader
    from modules.wifi_capture import WiFiCapture
    from modules.bluetooth_capture import BluetoothCapture
    from modules.adsb_ingest import ADSBIngest
    from modules.fusion import FusionEngine
    from modules.airspace_manager import AirspaceManager
    from modules.proximity_alert import ProximityAlertMonitor
except ImportError as e:
    print(f"❌ Missing module: {e}\n"
          f"   Check that all files under modules/ exist per the project layout.")
    sys.exit(1)

LOG = logging.getLogger("main")


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
def setup_logging(debug: bool, log_file: str = "airspace_tool.log"):
    level = logging.DEBUG if debug else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    handlers = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(log_file))
    except OSError:
        # Non-fatal: continue with console-only logging if file isn't writable
        pass

    logging.basicConfig(level=level, format=fmt, handlers=handlers)
    LOG.info("Logging initialised (debug=%s)", debug)


# ---------------------------------------------------------------------------
# Preflight checks — fail fast on missing hardware/data rather than crash
# mid-flight in the GUI thread
# ---------------------------------------------------------------------------
def preflight_checks(config: dict) -> list:
    """Return a list of warnings (non-fatal). Raises on fatal misconfig."""
    warnings = []

    geojson_path = Path(config.get("geofences", {}).get("path",
                         "data/geofences/uk_zones.geojson"))
    if not geojson_path.exists():
        warnings.append(f"⚠️ Geofence file missing: {geojson_path}")

    tiles_path = Path(config.get("map", {}).get("offline_tile_dir", "data/tiles"))
    if not tiles_path.exists():
        warnings.append(f"⚠️ Offline tile cache dir missing: {tiles_path} "
                         f"(will attempt online-only mode)")

    gps_device = config.get("gps", {}).get("device", "/dev/ttyAMA0")
    if not Path(gps_device).exists():
        warnings.append(f"⚠️ GPS device not found: {gps_device}")

    wifi_iface = config.get("wifi", {}).get("monitor_interface")
    if wifi_iface is None:
        warnings.append("⚠️ No WiFi monitor interface configured — "
                         "passive WiFi detection disabled")

    return warnings


# ---------------------------------------------------------------------------
# Application orchestrator
# ---------------------------------------------------------------------------
class AirspaceToolOrchestrator:
    """Owns lifecycle of all background workers. GUI queries this object."""

    def __init__(self, config: dict):
        self.config = config
        self._stop_event = threading.Event()

        # --- Core shared state ---
        self.fusion = FusionEngine(config=config)

        # --- Data acquisition workers ---
        self.gps_reader = GPSReader(
            device=config["gps"]["device"],
            baud=config["gps"].get("baud", 9600),
            on_fix=self.fusion.update_gps_fix,
        )

        self.wifi_capture = None
        if config.get("wifi", {}).get("enabled", True):
            self.wifi_capture = WiFiCapture(
                interface=config["wifi"].get("monitor_interface"),
                on_detection=self.fusion.update_remoteid_track,
            )

        self.bt_capture = None
        if config.get("bluetooth", {}).get("enabled", True):
            self.bt_capture = BluetoothCapture(
                on_detection=self.fusion.update_remoteid_track,
                scan_interval=config["bluetooth"].get("scan_interval_s", 5),
            )

        self.adsb_ingest = ADSBIngest(
            url=config["adsb"].get("url", "http://localhost/tar1090/data/aircraft.json"),
            poll_interval=config["adsb"].get("poll_interval_s", 2),
            on_update=self.fusion.update_adsb_tracks,
        )

        # --- Airspace / NOTAM / NFZ management ---
        self.airspace_manager = AirspaceManager(
            config=config,
            on_zones_updated=self.fusion.update_geofence_zones,
        )

        # --- Proximity alerts (unverified zones near GPS) ---
        self.proximity_monitor = None
        if config.get("airspace", {}).get("proximity_alert", {}).get("enabled", False):
            pa_cfg = config["airspace"]["proximity_alert"]
            self.proximity_monitor = ProximityAlertMonitor(
                get_gps_fix=self.fusion.get_gps_fix,
                get_zones=self.airspace_manager.get_current_zones,
                radius_m=pa_cfg.get("radius_m", 5000),
                check_interval_s=pa_cfg.get("check_interval_s", 15),
                on_alert=self.fusion.update_proximity_alerts,
            )

        self._threads = []

    # -- lifecycle -----------------------------------------------------
    def start(self):
        LOG.info("Starting background workers...")

        self._threads.append(self._spawn(self.gps_reader.run, "gps_reader"))

        if self.wifi_capture:
            self._threads.append(self._spawn(self.wifi_capture.run, "wifi_capture"))
        else:
            LOG.warning("WiFi capture disabled/unavailable")

        if self.bt_capture:
            self._threads.append(self._spawn(self.bt_capture.run_async_loop, "bt_capture"))
        else:
            LOG.warning("Bluetooth capture disabled/unavailable")

        self._threads.append(self._spawn(self.adsb_ingest.run, "adsb_ingest"))
        self._threads.append(self._spawn(self.airspace_manager.run_scheduler, "airspace_mgr"))

        if self.proximity_monitor:
            self._threads.append(self._spawn(self.proximity_monitor.run, "proximity_alert"))

        LOG.info("All workers started (%d threads).", len(self._threads))

    def _spawn(self, target, name):
        t = threading.Thread(target=target, args=(self._stop_event,),
                              name=name, daemon=True)
        t.start()
        return t

    def stop(self):
        LOG.info("Shutdown requested — signalling workers to stop...")
        self._stop_event.set()

        for worker in (self.gps_reader, self.wifi_capture, self.bt_capture,
                        self.adsb_ingest, self.airspace_manager, self.proximity_monitor):
            if worker and hasattr(worker, "stop"):
                try:
                    worker.stop()
                except Exception as e:
                    LOG.warning("Error stopping %s: %s", worker, e)

        for t in self._threads:
            t.join(timeout=5)
            if t.is_alive():
                LOG.warning("Thread %s did not stop cleanly within timeout", t.name)

        LOG.info("Shutdown complete.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(description="Airspace Awareness Tool")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--no-gui", action="store_true",
                         help="Run backend only (headless), no Kivy GUI")
    return parser.parse_args()


def main():
    args = parse_args()
    setup_logging(debug=args.debug)

    LOG.info("Loading configuration from %s", args.config)
    config = load_config(args.config)

    warnings = preflight_checks(config)
    for w in warnings:
        LOG.warning(w)

    orchestrator = AirspaceToolOrchestrator(config)

    # --- Signal handling for graceful shutdown ---
    def handle_signal(signum, frame):
        LOG.info("Received signal %s", signum)
        orchestrator.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    orchestrator.start()

    if args.no_gui:
        LOG.info("Running headless (--no-gui). Press Ctrl+C to exit.")
        try:
            signal.pause()
        except KeyboardInterrupt:
            orchestrator.stop()
        return

    # --- Launch Kivy GUI (blocks until window closes) ---
    try:
        from gui.app import AirspaceApp
    except ImportError as e:
        LOG.error("GUI module missing: %s — falling back to headless mode", e)
        signal.pause()
        return

    app = AirspaceApp(
        config=config,
        fusion=orchestrator.fusion,
        airspace_manager=orchestrator.airspace_manager,
    )
    try:
        app.run()
    finally:
        orchestrator.stop()


if __name__ == "__main__":
    main()
