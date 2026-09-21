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


def time_weighted_mean(points: list[tuple[dt.datetime, float]],
                       end: dt.datetime) -> float | None:
    """Mean of a step-function series, weighting each reading by its duration.

    Home Assistant stores state *changes*, so samples are not evenly spaced: a
    turbulent hour (a shower) emits far more rows than a calm one. Averaging
    the rows would weight that hour far above its share of the day; weighting
    by how long each value actually held gives the true daily mean.
    """
    if not points:
        return None
    points = sorted(points, key=lambda p: p[0])
    total = 0.0
    total_weight = 0.0
    for i, (stamp, value) in enumerate(points):
        stop = points[i + 1][0] if i + 1 < len(points) else end
        weight = max((stop - stamp).total_seconds(), 0.0)
        total += value * weight
        total_weight += weight
    if total_weight <= 0:  # all samples at one instant
        return sum(v for _, v in points) / len(points)
    return total / total_weight


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
                 temp_is_fahrenheit: bool = True, timeout: int = 10,
                 average_hours: float = 24.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.temp_entity = temp_entity
        self.humidity_entity = humidity_entity
        self.lux_entity = lux_entity
        self.temp_is_fahrenheit = temp_is_fahrenheit
        self.timeout = timeout
        # Average over this many hours instead of taking a spot reading. A
        # thermostat reports the moment it is asked: showers push indoor RH from
        # ~60% to ~84% for an hour or two, and the 14:00 run landed squarely on
        # that peak. Plants integrate conditions over days, so a daily mean is
        # both more stable and more physically meaningful. 0 disables.
        self.average_hours = average_hours

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def _state(self, entity_id: str) -> float:
        r = requests.get(
            f"{self.base_url}/api/states/{entity_id}",
            headers=self._headers(), timeout=self.timeout,
        )
        r.raise_for_status()
        return float(r.json()["state"])

    def _mean(self, entity_id: str, hours: float) -> float | None:
        """Time-weighted mean of an entity over the last ``hours``.

        Time-weighted, not a plain mean over samples: Home Assistant stores
        state *changes*, so a volatile hour produces many more rows than a calm
        one and a naive average would be dragged toward whatever was changing
        fastest -- here, the shower. Each reading is instead weighted by how
        long it actually held.
        """
        now = dt.datetime.now(dt.timezone.utc)
        start = now - dt.timedelta(hours=hours)
        try:
            r = requests.get(
                f"{self.base_url}/api/history/period/{start.isoformat()}",
                headers=self._headers(),
                params={"filter_entity_id": entity_id, "minimal_response": "true"},
                timeout=self.timeout,
            )
            r.raise_for_status()
            payload = r.json()
        except Exception:
            return None

        series = payload[0] if payload else []
        points: list[tuple[dt.datetime, float]] = []
        for s in series:
            stamp = s.get("last_changed") or s.get("last_updated")
            try:
                points.append((dt.datetime.fromisoformat(stamp), float(s["state"])))
            except (TypeError, ValueError, KeyError):
                continue  # 'unavailable', 'unknown', malformed rows
        if not points:
            return None

        return time_weighted_mean(points, now)

    def _value(self, entity_id: str) -> float:
        """Averaged value when history is available, else the spot reading."""
        if self.average_hours > 0:
            mean = self._mean(entity_id, self.average_hours)
            if mean is not None:
                return mean
        return self._state(entity_id)

    def conditions(self, date: dt.date | None = None) -> Conditions:
        temp = self._value(self.temp_entity)
        if self.temp_is_fahrenheit:
            temp = (temp - 32.0) * 5.0 / 9.0
        humidity = self._value(self.humidity_entity)
        lux = None
        if self.lux_entity:
            try:
                lux = self._value(self.lux_entity)
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
            average_hours=float(os.environ.get("HA_AVERAGE_HOURS", "24")),
        )
        try:
            ha.conditions()  # probe so we can fall back cleanly
            window = ha.average_hours
            label = (f"Home Assistant ({window:g}h average)" if window > 0
                     else "Home Assistant (spot reading)")
            return ha, label
        except Exception as e:
            return static, f"StaticClimate (HA unreachable: {e})"

    return static, "StaticClimate (HA not configured)"
