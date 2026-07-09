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
