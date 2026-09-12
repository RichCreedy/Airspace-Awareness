"""
Manual NOTAM import — parses a user-supplied file into a GeoJSON
zone feature, flagged verified=False until manually confirmed.
"""
import json
import os
import logging
import uuid

log = logging.getLogger(__name__)


def import_notam_file(input_path, output_dir):
    """
    Accepts a simple structured NOTAM text/JSON file and converts it
    to a GeoJSON feature. Extend _parse_raw() for real NOTAM text format.
    """
    with open(input_path, "r") as f:
        content = f.read()

    feature = _parse_raw(content)
    os.makedirs(output_dir, exist_ok=True)
    out_name = f"notam_{uuid.uuid4().hex[:8]}.geojson"
    out_path = os.path.join(output_dir, out_name)

    geojson = {"type": "FeatureCollection", "features": [feature]}
    with open(out_path, "w") as f:
        json.dump(geojson, f, indent=2)

    log.info("Imported NOTAM -> %s", out_path)
    return out_path


def _parse_raw(content):
    """
    TODO: Replace with real NOTAM text parsing (ICAO format).
    For now expects JSON: {"name": ..., "coordinates": [[lon,lat], ...]}
    """
    data = json.loads(content)
    return {
        "type": "Feature",
        "properties": {
            "name": data.get("name", "Imported NOTAM"),
            "verified": False,
            "type": "notam",
            "source": "manual_import",
        },
        "geometry": {
            "type": "Polygon",
            "coordinates": [data.get("coordinates", [])],
        },
    }
