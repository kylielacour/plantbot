#!/usr/bin/env python3
"""Plant storage.

Two clearly separated concerns:

  * plants.yaml          -- human-owned config (pot geometry, species pid,
                            light, etc). Never written by the tool, so your
                            comments and formatting are preserved.
  * state/watering_state.json -- machine-owned. Records last_watered per plant
                            id (written by the sync script). Keeps config and
                            runtime state from clobbering each other.

The abstract PlantStore leaves room to re-add a NotionPlantStore later; today
only YamlPlantStore is implemented.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from watering_model import Plant, SpeciesData

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_PLANTS = BASE_DIR / "plants.yaml"
DEFAULT_STATE = BASE_DIR / "state" / "watering_state.json"

# Species fields a user may override per-plant in plants.yaml under `overrides:`.
_OVERRIDABLE = {
    "min_soil_moist", "max_soil_moist",
    "min_temp", "max_temp",
    "min_env_humid", "max_env_humid",
    "min_light_lux", "max_light_lux",
}


@dataclass
class PlantEntry:
    plant: Plant
    pid: str | None = None
    overrides: dict[str, float] = field(default_factory=dict)

    def species_with_overrides(self, base: SpeciesData | None) -> SpeciesData | None:
        """Merge per-plant overrides on top of OPB species data."""
        if not self.overrides:
            return base
        merged = SpeciesData(**vars(base)) if base else SpeciesData()
        for k, v in self.overrides.items():
            if k in _OVERRIDABLE:
                setattr(merged, k, float(v))
        return merged


def _to_float(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    return float(value)


def _soil_volume_ml(raw: dict[str, Any]) -> float:
    """Prefer an explicit soil_volume_ml; otherwise compute from cylinder dims."""
    if raw.get("soil_volume_ml") is not None:
        return float(raw["soil_volume_ml"])
    d = _to_float(raw.get("inner_diameter_cm"))
    h = _to_float(raw.get("soil_depth_cm"))
    if d and h:
        radius_cm = d / 2.0
        return 3.141592653589793 * radius_cm * radius_cm * h  # cm^3 == ml
    raise ValueError(
        f"plant '{raw.get('id', '?')}' needs soil_volume_ml or "
        f"inner_diameter_cm + soil_depth_cm"
    )


def plant_entry_from_dict(raw: dict[str, Any]) -> PlantEntry:
    """Build a PlantEntry from a raw dict (from YAML or the server API)."""
    plant = Plant(
        id=str(raw["id"]),
        name=str(raw.get("name", raw["id"])),
        soil_volume_ml=_soil_volume_ml(raw),
        soil_type=str(raw.get("soil_type", "standard")),
        light=str(raw.get("light", "medium")),
        light_lux=_to_float(raw.get("light_lux")),
        water_use=str(raw.get("water_use", "medium")),
        growth_state=str(raw.get("growth_state", "auto")),
        has_drainage=bool(raw.get("has_drainage", True)),
    )
    overrides = {
        k: float(v) for k, v in (raw.get("overrides") or {}).items()
        if k in _OVERRIDABLE and v is not None
    }
    return PlantEntry(
        plant=plant,
        pid=(str(raw["pid"]) if raw.get("pid") else None),
        overrides=overrides,
    )


def load_plants(path: Path = DEFAULT_PLANTS) -> list[PlantEntry]:
    data = yaml.safe_load(Path(path).read_text()) or {}
    return [plant_entry_from_dict(raw) for raw in data.get("plants", [])]


def save_plants_raw(raw_plants: list[dict[str, Any]], path: Path = DEFAULT_PLANTS) -> None:
    """Atomically write the plant list back to plants.yaml (used by the editor)."""
    path = Path(path)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump({"plants": raw_plants}, sort_keys=False, allow_unicode=True))
    tmp.replace(path)


class WateringState:
    """last_watered per plant id, persisted to state/watering_state.json."""

    def __init__(self, path: Path = DEFAULT_STATE):
        self.path = Path(path)
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except Exception:
                return {}
        return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True))
        tmp.replace(self.path)

    def get_last_watered(self, plant_id: str) -> dt.date | None:
        rec = self._data.get(plant_id)
        if rec and rec.get("last_watered"):
            try:
                return dt.date.fromisoformat(rec["last_watered"][:10])
            except ValueError:
                return None
        return None

    def set_last_watered(self, plant_id: str, date: dt.date) -> None:
        self._data.setdefault(plant_id, {})["last_watered"] = date.isoformat()
        self._save()


class UrlPlantStore:
    """Reads plants + watering state from a remote plantserver (on benedict).

    Lets the Mac's watering job use benedict as the source of truth: it fetches
    the plant list over HTTP and reports completed waterings back. Implements the
    same get/set_last_watered interface as WateringState.
    """

    def __init__(self, base_url: str, timeout: int = 15):
        import requests  # local import so the server side doesn't require it here
        self._requests = requests
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._state: dict[str, Any] | None = None

    def load_plants(self) -> list[PlantEntry]:
        r = self._requests.get(f"{self.base_url}/api/plants", timeout=self.timeout)
        r.raise_for_status()
        return [plant_entry_from_dict(raw) for raw in r.json().get("plants", [])]

    def _load_state(self) -> dict[str, Any]:
        if self._state is None:
            r = self._requests.get(f"{self.base_url}/api/state", timeout=self.timeout)
            r.raise_for_status()
            self._state = r.json() or {}
        return self._state

    def get_last_watered(self, plant_id: str) -> dt.date | None:
        rec = self._load_state().get(plant_id)
        if rec and rec.get("last_watered"):
            try:
                return dt.date.fromisoformat(rec["last_watered"][:10])
            except ValueError:
                return None
        return None

    def set_last_watered(self, plant_id: str, date: dt.date) -> None:
        r = self._requests.post(
            f"{self.base_url}/api/last_watered",
            json={"plant_id": plant_id, "date": date.isoformat()},
            timeout=self.timeout,
        )
        r.raise_for_status()
        if self._state is not None:
            self._state.setdefault(plant_id, {})["last_watered"] = date.isoformat()


def make_stores():
    """Return (plant_entries, watering_state) from benedict if PLANTBOT_SERVER_URL
    is set, else from local files. Both objects expose get/set_last_watered."""
    import os
    server = os.environ.get("PLANTBOT_SERVER_URL")
    if server:
        store = UrlPlantStore(server)
        return store.load_plants(), store
    return load_plants(), WateringState()
