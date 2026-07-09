#!/usr/bin/env python3
"""Open Plantbook API client with local caching.

Open Plantbook (https://open.plantbook.io) is a free, crowd-sourced database of
per-species care thresholds. We use it to populate the soil-moisture band and
comfort ranges that drive the watering model.

Auth is OAuth2 client-credentials: generate a client_id / client_secret in the
OPB web UI under "API keys" and put them in .env as OPB_CLIENT_ID /
OPB_CLIENT_SECRET.

Species detail is cached to state/species_cache.json — the data rarely changes,
it keeps us within OPB's rate limits, and it lets the tool run offline.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import requests

from watering_model import SpeciesData

API_BASE = "https://open.plantbook.io/api/v1"
TOKEN_URL = f"{API_BASE}/token/"
SEARCH_URL = f"{API_BASE}/plant/search"
DETAIL_URL = f"{API_BASE}/plant/detail"

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CACHE = BASE_DIR / "state" / "species_cache.json"

# Fields we pull from a detail response into SpeciesData.
_DETAIL_FIELDS = (
    "min_soil_moist", "max_soil_moist",
    "min_temp", "max_temp",
    "min_env_humid", "max_env_humid",
    "min_light_lux", "max_light_lux",
)


class OpenPlantbook:
    def __init__(self, client_id: str, client_secret: str,
                 cache_path: Path = DEFAULT_CACHE, timeout: int = 30):
        self.client_id = client_id
        self.client_secret = client_secret
        self.cache_path = Path(cache_path)
        self.timeout = timeout
        self._token: str | None = None
        self._cache = self._load_cache()

    @classmethod
    def from_env(cls, cache_path: Path = DEFAULT_CACHE) -> "OpenPlantbook | None":
        cid = os.environ.get("OPB_CLIENT_ID")
        secret = os.environ.get("OPB_CLIENT_SECRET")
        if not cid or not secret:
            return None
        return cls(cid, secret, cache_path=cache_path)

    # ------------------------------------------------------------- caching
    def _load_cache(self) -> dict[str, Any]:
        if self.cache_path.exists():
            try:
                return json.loads(self.cache_path.read_text())
            except Exception:
                return {}
        return {}

    def _save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._cache, indent=2, sort_keys=True))
        tmp.replace(self.cache_path)

    # ---------------------------------------------------------------- auth
    def _get_token(self) -> str:
        if self._token:
            return self._token
        r = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=self.timeout,
        )
        r.raise_for_status()
        self._token = r.json()["access_token"]
        return self._token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._get_token()}"}

    # ---------------------------------------------------------------- calls
    def search(self, query: str) -> list[dict[str, Any]]:
        r = requests.get(
            SEARCH_URL, headers=self._headers(),
            params={"alias": query}, timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json().get("results", [])

    def detail_raw(self, pid: str, refresh: bool = False) -> dict[str, Any]:
        """Full detail dict for a pid, using the local cache when possible."""
        if not refresh and pid in self._cache:
            return self._cache[pid]

        r = requests.get(
            f"{DETAIL_URL}/{requests.utils.quote(pid)}/",
            headers=self._headers(),
            params={"include": "care"},
            timeout=self.timeout,
        )
        r.raise_for_status()
        data = r.json()
        data["_cached_at"] = time.time()
        self._cache[pid] = data
        self._save_cache()
        return data

    def species_data(self, pid: str, refresh: bool = False) -> SpeciesData:
        raw = self.detail_raw(pid, refresh=refresh)
        return species_data_from_detail(raw)

    def cached_species_data(self, pid: str) -> SpeciesData | None:
        """SpeciesData from cache only (no network). None if not cached."""
        raw = self._cache.get(pid)
        return species_data_from_detail(raw) if raw else None


def species_data_from_detail(raw: dict[str, Any]) -> SpeciesData:
    def num(key: str) -> float | None:
        v = raw.get(key)
        return float(v) if isinstance(v, (int, float)) else None

    return SpeciesData(**{f: num(f) for f in _DETAIL_FIELDS})
