"""
Fetches remote airspace/NOTAM data (e.g. OpenAIP) and writes to disk.
Updates sync_metadata on success/failure for the freshness badge.
"""
import os
import json
import logging
import requests

from src.airspace.sync_metadata import SyncMetadata

log = logging.getLogger(__name__)


class OpenAIPSync:
    def __init__(self, endpoint, api_key, output_dir, metadata: SyncMetadata):
        self.endpoint = endpoint
        self.api_key = api_key
        self.output_dir = output_dir
        self.metadata = metadata

    def sync(self):
        try:
            headers = {"x-openaip-api-key": self.api_key} if self.api_key else {}
            resp = requests.get(self.endpoint, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            os.makedirs(self.output_dir, exist_ok=True)
            out_path = os.path.join(self.output_dir, "openaip_raw.json")
            with open(out_path, "w") as f:
                json.dump(data, f)

            self.metadata.record_success("openaip")
            log.info("OpenAIP sync succeeded -> %s", out_path)
            return True
        except Exception as e:
            self.metadata.record_failure("openaip", str(e))
            log.warning("OpenAIP sync failed: %s", e)
            return False
