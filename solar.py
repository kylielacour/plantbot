#!/usr/bin/env python3
"""Day-length / season helpers.

Pure functions only (no I/O) so they are trivially unit-testable. Used to
modulate transpiration by season and to auto-detect dormancy for plants whose
growth state is set to ``auto``.
"""
from __future__ import annotations

import datetime as dt
import math

# Axial tilt of the Earth, degrees.
_OBLIQUITY_DEG = 23.45


def day_of_year(date: dt.date) -> int:
    return date.timetuple().tm_yday


def solar_declination_deg(date: dt.date) -> float:
    """Approximate solar declination (Cooper's equation), in degrees."""
    n = day_of_year(date)
    return _OBLIQUITY_DEG * math.sin(math.radians(360.0 * (284 + n) / 365.0))


def day_length_hours(date: dt.date, latitude_deg: float) -> float:
    """Hours of daylight for a given date and latitude.

    Clamped to [0, 24] to handle polar day / night gracefully.
    """
    phi = math.radians(latitude_deg)
    decl = math.radians(solar_declination_deg(date))

    cos_omega = -math.tan(phi) * math.tan(decl)
    if cos_omega >= 1.0:
        return 0.0  # polar night
    if cos_omega <= -1.0:
        return 24.0  # polar day

    omega = math.acos(cos_omega)  # radians
    # Sunrise hour angle in degrees -> total daylight hours.
    return 2.0 * math.degrees(omega) / 15.0


def noon_altitude_deg(date: dt.date, latitude_deg: float) -> float:
    """Solar altitude at local noon, in degrees (negative during polar night)."""
    return 90.0 - abs(latitude_deg - solar_declination_deg(date))


def _mean_noon_sin(latitude_deg: float) -> float:
    """Annual mean of sin(noon altitude) at a latitude."""
    year = dt.date(2001, 1, 1)  # non-leap reference year
    total = 0.0
    for i in range(365):
        alt = noon_altitude_deg(year + dt.timedelta(days=i), latitude_deg)
        total += math.sin(math.radians(max(alt, 0.0)))
    return total / 365.0


_MEAN_NOON_SIN_CACHE: dict[float, float] = {}


def solar_intensity_factor(date: dt.date, latitude_deg: float) -> float:
    """How strong light at a fixed indoor spot is *right now* vs its annual average.

    Day length already accounts for the *hours* of light (see
    watering_model.season_factor). This is the other half: in winter the sun
    sits low, so the same window delivers weaker light per hour. Driven by
    sin(noon altitude), the standard geometric term for how much solar energy
    lands on a surface.

    Normalised so it averages 1.0 over the year -- the placement lux table is
    calibrated as a year-round average, and this must swing around that
    calibration rather than shift it.
    """
    key = round(latitude_deg, 3)
    mean = _MEAN_NOON_SIN_CACHE.get(key)
    if mean is None:
        mean = _mean_noon_sin(latitude_deg)
        _MEAN_NOON_SIN_CACHE[key] = mean
    if mean <= 0:
        return 1.0
    alt = noon_altitude_deg(date, latitude_deg)
    return max(math.sin(math.radians(max(alt, 0.0))) / mean, 0.05)
