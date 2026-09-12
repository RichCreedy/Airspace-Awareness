"""
auto_sync_scheduler.py

Background scheduler for automated airspace data refresh:
  - Periodic OpenAIP sync
  - mtime-triggered FRZ regeneration (not timer-based)
  - Expired manual NOTAM zone pruning
  - Exponential backoff on repeated failures
  - Manual retry hook for GUI "Retry now" buttons
"""
import threading
import logging

logger = logging.getLogger("auto_sync_scheduler")


class AutoSyncScheduler:
    def __init__(self, openaip_sync=None, sync_metadata=None,
                 aerodromes_path="data/aerodromes/aerodromes.yaml",
                 frz_output_path="data/geofences/frz_zones.geojson",
                 manual_zones_path="data/geofences/manual_zones.geojson",
                 base_interval_s=3600, max_interval_s=6 * 3600,
                 backoff_multiplier=2.0):
        self.openaip_sync = openaip_sync
        self.sync_metadata = sync_metadata
        self.aerodromes_path = aerodromes_path
        self.frz_output_path = frz_output_path
        self.manual_zones_path = manual_zones_path

        self.base_interval_s = base_interval_s
        self.max_interval_s = max_interval_s
        self.backoff_multiplier = backoff_multiplier
        self._current_interval_s = base_interval_s

        self._stop_event = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("AutoSyncScheduler started")

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("AutoSyncScheduler stopped")

    def _run_loop(self):
        while not self._stop_event.is_set():
            success = self._run_sync_cycle()
            with self._lock:
                self._current_interval_s = (
                    self.base_interval_s if success
                    else min(self._current_interval_s * self.backoff_multiplier, self.max_interval_s)
                )
            self._stop_event.wait(self._current_interval_s)

    def _run_sync_cycle(self):
        all_ok = True
        if self.openaip_sync:
            all_ok &= self._check_and_sync_one()
        all_ok &= self._check_frz_regen()
        try:
            import modules.notam_import as notam_import
            notam_import.prune_expired(path=self.manual_zones_path)
        except Exception as exc:  # pragma: no cover
            logger.warning("NOTAM prune failed: %s", exc)
            all_ok = False
        return all_ok

    def _check_and_sync_one(self):
        try:
            self.openaip_sync.sync_all()
            return True
        except Exception as exc:
            logger.warning("OpenAIP sync failed: %s", exc)
            return False

    def _check_frz_regen(self):
        try:
            from modules.frz_generator import regenerate_if_needed
            result = regenerate_if_needed(
                source_path=self.aerodromes_path,
                output_path=self.frz_output_path,
                sync_metadata=self.sync_metadata,
            )
            if result is not None:
                logger.info("FRZ zones regenerated (%d features)", len(result.get("features", [])))
            return True
        except Exception as exc:
            logger.warning("FRZ regeneration failed: %s", exc)
            return False

    def retry_now(self, source=None):
        def _do_retry():
            if source == "openaip_sync":
                self._check_and_sync_one()
            elif source == "frz_generation":
                self._check_frz_regen()
            else:
                self._run_sync_cycle()
        threading.Thread(target=_do_retry, daemon=True).start()
