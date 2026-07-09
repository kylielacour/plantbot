#!/usr/bin/env python3
"""Climate sources.

Provides current temperature + humidity (and optional lux) to the watering
model. ``HomeAssistantClimate`` uses the user's existing thermostat data; if HA
is not configured or unreachable, ``StaticClimate`` supplies sensible indoor
defaults so the tool always works.
"""
from __future__ import annotations

import datetime as dt
import os

import requests

import solar
from watering_model import Conditions, clamp


class StaticClimate:
    """Fixed indoor climate with a mild seasonal nudge (drier/warmer swings).

    Defaults model a typical heated/cooled home: ~21 C, ~45% RH. Humidity is
    nudged down a little in winter (heating dries the air) and up in summer.
    """

    def __init__(self, temp_c: float = 21.0, humidity_pct: float = 45.0,
                 latitude_deg: float = 40.0):
        self.temp_c = temp_c
        self.humidity_pct = humidity_pct
        self.latitude_deg = latitude_deg

    def conditions(self, date: dt.date | None = None) -> Conditions:
        date = date or dt.date.today()
        day_len = solar.day_length_hours(date, self.latitude_deg)
        # +/- ~7% RH between deep winter (short days) and midsummer.
        humidity = self.humidity_pct + (day_len - 12.0) * 1.2
        return Conditions(
            temp_c=self.temp_c,
            humidity_pct=clamp(humidity, 15.0, 90.0),
            lux=None,
        )


class HomeAssistantClimate:
    """Live temperature + humidity (+ optional lux) from Home Assistant."""

    def __init__(self, base_url: str, token: str, temp_entity: str,
                 humidity_entity: str, lux_entity: str | None = None,
                 temp_is_fahrenheit: bool = True, timeout: int = 10):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.temp_entity = temp_entity
        self.humidity_entity = humidity_entity
        self.lux_entity = lux_entity
        self.temp_is_fahrenheit = temp_is_fahrenheit
        self.timeout = timeout

    def _state(self, entity_id: str) -> float:
        r = requests.get(
            f"{self.base_url}/api/states/{entity_id}",
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=self.timeout,
        )
        r.raise_for_status()
        return float(r.json()["state"])

    def conditions(self, date: dt.date | None = None) -> Conditions:
        temp = self._state(self.temp_entity)
        if self.temp_is_fahrenheit:
            temp = (temp - 32.0) * 5.0 / 9.0
        humidity = self._state(self.humidity_entity)
        lux = None
        if self.lux_entity:
            try:
                lux = self._state(self.lux_entity)
            except Exception:
                lux = None
        return Conditions(temp_c=temp, humidity_pct=humidity, lux=lux)


def from_env() -> tuple[object, str]:
    """Build the best available climate source from environment variables.

    Prefers Home Assistant (thermostat data the user already has); falls back to
    StaticClimate if HA is unconfigured or the first read fails. Returns
    ``(source, description)``.
    """
    latitude = float(os.environ.get("LATITUDE", "40"))

    ha_url = os.environ.get("HA_URL")
    ha_token = os.environ.get("HA_TOKEN")
    temp_entity = os.environ.get("HA_TEMP_ENTITY")
    hum_entity = os.environ.get("HA_HUMIDITY_ENTITY")

    static = StaticClimate(latitude_deg=latitude)

    if ha_url and ha_token and temp_entity and hum_entity:
        ha = HomeAssistantClimate(
            base_url=ha_url,
            token=ha_token,
            temp_entity=temp_entity,
            humidity_entity=hum_entity,
            lux_entity=os.environ.get("HA_LUX_ENTITY") or None,
            temp_is_fahrenheit=os.environ.get("HA_TEMP_UNIT", "F").upper().startswith("F"),
        )
        try:
            ha.conditions()  # probe so we can fall back cleanly
            return ha, "Home Assistant (live thermostat)"
        except Exception as e:
            return static, f"StaticClimate (HA unreachable: {e})"

    return static, "StaticClimate (HA not configured)"
