#!/usr/bin/env python3
"""
main.py — Orchestrator entry point for the uConsole Airspace Awareness Tool

Startup order matters:
    1. FusionEngine        (must exist before anything feeds it data)
    2. Acquisition modules (GPS, ADS-B, WiFi, Bluetooth)
    3. AirspaceManager      (NOTAM/NFZ sync + FRZ regeneration)
    4. ProximityAlertMonitor (depends on fusion having GPS + zones)
    5. GUI (Kivy)           (runs on the main thread, blocks until closed)

Shutdown is the reverse order, and is idempotent — safe to call twice
(e.g. once from Kivy's on_stop() and once from a finally block).
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import signal
import sys
import threading
from pathlib import Path

import yaml

from modules.fusion import FusionEngine
from modules.gps_reader import GPSReader
from modules.adsb_ingest import ADSBIngest
from modules.wifi_capture import WiFiCapture
from modules.bluetooth_capture import BluetoothCapture
from modules.airspace_manager import AirspaceManager
from modules.proximity_alert_monitor import ProximityAlertMonitor

LOG = logging.getLogger("main")

DEFAULT_CONFIG_PATH = "config.yaml"
REQUIRED_DIRS = [
    "data/geofences",
    "data/tiles",
    "data/notam",
    "logs",
]


# ----------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------
def setup_logging(log_level: str, log_file: str | None):
    level = getattr(logging, log_level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s [%(threadName)s] %(name)s: %(message)s"
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=3
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)

    # Quiet down noisy third-party libs unless we're debugging
    if level > logging.DEBUG:
        logging.getLogger("bleak").setLevel(logging.WARNING)
        logging.getLogger("scapy").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)


# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------
def load_config(path: str) -> dict:
    cfg_path = Path(path)
    if not cfg_path.exists():
        LOG.error("Config file not found: %s", cfg_path)
        sys.exit(1)

    with open(cfg_path, "r") as f:
        config = yaml.safe_load(f) or {}

    # ⚠️ Minimal sanity check only — a proper config validation script
    # against a schema is still an open item (see OPEN_QUESTIONS).
    required_top_level = ["gps", "adsb", "wifi", "bluetooth", "airspace"]
    missing = [k for k in required_top_level if k not in config]
    if missing:
        LOG.warning("Config is missing top-level sections: %s — "
                     "affected modules will fall back to hardcoded defaults", missing)

    return config


def ensure_directories():
    for d in REQUIRED_DIRS:
        Path(d).mkdir(parents=True, exist_ok=True)


# ----------------------------------------------------------------------
# Orchestrator
# ----------------------------------------------------------------------
class Orchestrator:
    """
    Owns the lifecycle of every backend module. The GUI is given a
    reference to this object (not the other way around) so it can:
      - call orchestrator.fusion.get_snapshot() every render tick
      - call orchestrator.get_module_statuses() for the freshness badge
      - call orchestrator.airspace_manager.manual_retry() from a "retry" button
      - call orchestrator.stop() on window close
    """

    def __init__(self, config: dict):
        self.config = config
        self._started = False
        self._stopped = False
        self._lock = threading.Lock()

        # 1. Fusion hub — must exist before anything else is constructed,
        #    since every acquisition module's callback points into it.
        self.fusion = FusionEngine(config)

        # 2. Acquisition modules
        self.gps = GPSReader(config, on_fix=self.fusion.update_gps_fix)
        self.adsb = ADSBIngest(config, on_update=self.fusion.update_adsb_tracks)
        self.wifi = WiFiCapture(config, on_detection=self.fusion.update_remoteid_track)
        self.bluetooth = BluetoothCapture(config, on_detection=self.fusion.update_remoteid_track)

        # 3. Airspace / NOTAM / FRZ management
        #    on_zones_updated feeds parsed GeoJSON features straight into fusion.
        #    ⚠️ Assumed interface — see flags below main.py listing.
        self.airspace_manager = AirspaceManager(
            config,
            on_zones_updated=self.fusion.update_zones,
        )

        # 4. Proximity alerting — reads GPS fix + zones from fusion,
        #    writes alerts back into fusion for the GUI banner to consume.
        self.proximity_monitor = ProximityAlertMonitor(
            config,
            fusion=self.fusion,
        )

        self._acquisition_modules = [self.gps, self.adsb, self.wifi, self.bluetooth]

    # ------------------------------------------------------------------
    def start(self):
        with self._lock:
            if self._started:
                LOG.warning("Orchestrator.start() called twice — ignoring")
                return
            self._started = True

        LOG.info("Starting FusionEngine...")
        self.fusion.start()

        LOG.info("Starting acquisition modules...")
        for mod in self._acquisition_modules:
            try:
                mod.start()
            except Exception:
                LOG.exception("Failed to start %s — continuing without it",
                               type(mod).__name__)

        LOG.info("Starting AirspaceManager...")
        try:
            self.airspace_manager.start()
        except Exception:
            LOG.exception("Failed to start AirspaceManager")

        LOG.info("Starting ProximityAlertMonitor...")
        try:
            self.proximity_monitor.start()
        except Exception:
            LOG.exception("Failed to start ProximityAlertMonitor")

        LOG.info("Orchestrator started")

    def stop(self):
        with self._lock:
            if self._stopped or not self._started:
                return
            self._stopped = True

        LOG.info("Stopping ProximityAlertMonitor...")
        self._safe_stop(self.proximity_monitor)

        LOG.info("Stopping AirspaceManager...")
        self._safe_stop(self.airspace_manager)

        LOG.info("Stopping acquisition modules...")
        for mod in self._acquisition_modules:
            self._safe_stop(mod)

        LOG.info("Stopping FusionEngine...")
        self._safe_stop(self.fusion)

        LOG.info("Orchestrator stopped cleanly")

    @staticmethod
    def _safe_stop(module):
        try:
            module.stop()
        except Exception:
            LOG.exception("Error stopping %s", type(module).__name__)

    # ------------------------------------------------------------------
    def get_module_statuses(self) -> dict:
        """
        Aggregated status dict for the freshness badge / settings screen.
        Every acquisition module + airspace_manager exposes get_status()
        with a consistent shape (see previous message's module set).
        """
        return {
            "gps": self.gps.get_status(),
            "adsb": self.adsb.get_status(),
            "wifi": self.wifi.get_status(),
            "bluetooth": self.bluetooth.get_status(),
            "airspace": self.airspace_manager.get_status(),
            "proximity": self.proximity_monitor.get_status(),
        }

    def manual_retry_airspace(self):
        """Wired to a 'retry' button in the freshness badge popup."""
        LOG.info("Manual airspace retry requested from GUI")
        self.airspace_manager.manual_retry()


# ----------------------------------------------------------------------
# Signal handling (headless / non-GUI shutdown path)
# ----------------------------------------------------------------------
_shutdown_event = threading.Event()


def _handle_signal(signum, frame):
    LOG.info("Received signal %s — requesting shutdown", signum)
    _shutdown_event.set()


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="uConsole Airspace Awareness Tool")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH,
                         help="Path to config.yaml")
    parser.add_argument("--log-level", default="INFO",
                         choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--log-file", default="logs/app.log",
                         help="Set to empty string to disable file logging")
    parser.add_argument("--no-gui", action="store_true",
                         help="Run backend only, no Kivy window "
                              "(useful for headless testing on dev hardware)")
    args = parser.parse_args()

    setup_logging(args.log_level, args.log_file or None)
    ensure_directories()

    LOG.info("Loading config from %s", args.config)
    config = load_config(args.config)

    orchestrator = Orchestrator(config)

    # SIGTERM/SIGINT handling for the headless path. When the GUI is
    # running, Kivy owns the main thread and its own window-close /
    # Ctrl+C handling is what triggers App.on_stop() -> orchestrator.stop()
    # instead — this handler is the fallback for --no-gui mode.
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    orchestrator.start()

    try:
        if args.no_gui:
            LOG.info("Running headless — Ctrl+C to exit")
            while not _shutdown_event.is_set():
                _shutdown_event.wait(timeout=1)
        else:
            # Deferred import: keep Kivy (and its GL/window setup) out of
            # the headless code path entirely, so --no-gui works on a
            # dev box without a display or GPU stack configured.
            from gui.app import AirspaceToolApp

            app = AirspaceToolApp(orchestrator=orchestrator, config=config)
            app.run()

    except KeyboardInterrupt:
        LOG.info("KeyboardInterrupt received")
    finally:
        orchestrator.stop()
        LOG.info("Shutdown complete")


if __name__ == "__main__":
    main()
