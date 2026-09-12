"""
Regenerates FRZ (Flight Restriction Zone) geojson only when source
data files change on disk (mtime comparison), not on a timer.
"""
import os
import json
import logging

log = logging.getLogger(__name__)


class FRZGenerator:
    def __init__(self, source_paths, output_path):
        self.source_paths = source_paths  # list of raw NOTAM/openAIP files
        self.output_path = output_path
        self._mtimes = {}

    def check_and_regenerate(self):
        changed = False
        for path in self.source_paths:
            if not os.path.exists(path):
                continue
            mtime = os.path.getmtime(path)
            if self._mtimes.get(path) != mtime:
                self._mtimes[path] = mtime
                changed = True

        if changed:
            self._regenerate()
        return changed

    def _regenerate(self):
        log.info("Source data changed — regenerating FRZ zones...")
        features = []
        for path in self.source_paths:
            if not os.path.exists(path):
                continue
            try:
                with open(path) as f:
                    raw = json.load(f)
                # TODO: convert raw NOTAM/openAIP structures into
                # GeoJSON Polygon features with verified=False flag
                features.extend(self._convert(raw))
            except Exception as e:
                log.warning("Failed to process %s: %s", path, e)

        out = {"type": "FeatureCollection", "features": features}
        with open(self.output_path, "w") as f:
            json.dump(out, f, indent=2)
        log.info("Wrote %d FRZ features to %s", len(features), self.output_path)

    def _convert(self, raw):
        # Placeholder conversion — customize per source schema
        return raw.get("features", [])
