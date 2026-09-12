"""
openaip_sync.py

Fetches airspace/aerodrome reference data from the OpenAIP API, caches
it under data/openaip/, and records success/failure to SyncMetadata.
Requires airspace.openaip.api_key in config.yaml.
"""
import os
import json
import requests

OPENAIP_BASE_URL = "https://api.core.openaip.net/api"
DEFAULT_CACHE_DIR = "data/openaip"


class OpenAIPSyncError(Exception):
    pass


class OpenAIPSync:
    def __init__(self, api_key, country="gb", cache_dir=DEFAULT_CACHE_DIR,
                 sync_metadata=None, timeout=20):
        self.api_key = api_key
        self.country = country
        self.cache_dir = cache_dir
        self.sync_metadata = sync_metadata
        self.timeout = timeout
        os.makedirs(self.cache_dir, exist_ok=True)

    def _headers(self):
        return {"x-openaip-api-key": self.api_key}

    def _fetch_paginated(self, endpoint, params=None):
        params = dict(params or {})
        params.setdefault("country", self.country)
        params.setdefault("limit", 1000)
        page = 1
        all_items = []
        while True:
            params["page"] = page
            resp = requests.get(f"{OPENAIP_BASE_URL}/{endpoint}",
                                 headers=self._headers(), params=params, timeout=self.timeout)
            if resp.status_code != 200:
                raise OpenAIPSyncError(
                    f"OpenAIP {endpoint} request failed: {resp.status_code} {resp.text[:200]}"
                )
            payload = resp.json()
            items = payload.get("items", [])
            all_items.extend(items)
            if page >= payload.get("totalPages", 1) or not items:
                break
            page += 1
        return all_items

    def sync_airspaces(self):
        try:
            items = self._fetch_paginated("airspaces")
            self._write_cache("airspaces.json", items)
            if self.sync_metadata:
                self.sync_metadata.record_success("openaip_sync", detail=f"{len(items)} airspaces")
            return items
        except (requests.RequestException, OpenAIPSyncError) as exc:
            if self.sync_metadata:
                self.sync_metadata.record_failure("openaip_sync", exc)
            raise

    def sync_aerodromes(self):
        try:
            items = self._fetch_paginated("airports")
            self._write_cache("aerodromes.json", items)
            if self.sync_metadata:
                self.sync_metadata.record_success("openaip_sync", detail=f"{len(items)} aerodromes")
            return items
        except (requests.RequestException, OpenAIPSyncError) as exc:
            if self.sync_metadata:
                self.sync_metadata.record_failure("openaip_sync", exc)
            raise

    def sync_all(self):
        return {"airspaces": self.sync_airspaces(), "aerodromes": self.sync_aerodromes()}

    def _write_cache(self, filename, items):
        path = os.path.join(self.cache_dir, filename)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(items, f)
        os.replace(tmp_path, path)
