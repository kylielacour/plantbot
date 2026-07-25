#!/usr/bin/env python3
"""Watering model — pure, unit-testable functions.

Design split:
  * Open Plantbook (OPB) supplies per-species *setpoints* (the soil-moisture
    band, light/temp/humidity comfort ranges).
  * This module supplies the *physics* that turns current conditions into a
    drying rate, then combines the two into an interval + a pour amount.

Everything here is deterministic and free of I/O so it can be tested directly.

The two quantities we compute:

  amount_ml   = (max_soil_moist - min_soil_moist)/100 * soil_volume_ml
                i.e. the real volume of water to refill the root zone from the
                "water me" point back to "full". Driven by pot geometry + OPB,
                NOT by sensors, so it works for every plant.

  interval    = amount_to_deplete / daily_water_loss
                daily_water_loss is an evapotranspiration estimate scaled by
                VPD (temp+humidity), light, season and growth state.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

import solar

# ------------------------------------------------------------------ constants
# Baseline reference climate: 21 C / 50% RH ("comfortable room").
_BASELINE_TEMP_C = 21.0
_BASELINE_RH = 50.0

# Evapotranspiration base rate, ml of water lost per day per ml of soil, under
# baseline conditions with all modifiers == 1.0. Split between the original
# (~7-day baseline) and the longer tuned value — a representative plant lands
# around ~8-9 days at baseline.
_ET_BASE_ML_PER_ML_SOIL = 0.0215

# Interval guard rails (days).
MIN_INTERVAL_DAYS = 2
MAX_INTERVAL_DAYS = 30

# Water-use maps garden.org's "Water Preferences" scale to model coefficients.
# Each state tunes BOTH frequency (via Kc + MAD) and amount (via pour fraction).
# low/medium/high are kept as aliases of dry/mesic/wet for back-compatibility.
#
# Kc = transpiration intensity (a cactus loses far less water than a fern).
WATER_USE_KC = {
    "dry": 0.30,        # cacti, succulents, sansevieria, ZZ
    "dry_mesic": 0.55,  # drought-tolerant, likes to dry between waterings
    "mesic": 1.00,      # most foliage (pothos, monstera)
    "wet_mesic": 1.25,  # likes consistent moisture
    "wet": 1.45,        # ferns, calathea, thirsty growers
    "low": 0.30, "medium": 1.00, "high": 1.45,
}

# MAD = management-allowed depletion: how far we let the root zone dry before
# rewatering (drives interval). Drought-lovers dry right out; wet-lovers stay damp.
MAD_BY_WATER_USE = {
    "dry": 0.90,
    "dry_mesic": 0.70,
    "mesic": 0.50,
    "wet_mesic": 0.40,
    "wet": 0.30,
    "low": 0.90, "medium": 0.50, "high": 0.30,
}

# Measured-pour size as a fraction of soil volume, by water preference (drives
# amount): drier-preference plants get a smaller pour, wet-lovers a bigger soak.
POUR_FRACTION_BY_WATER_USE = {
    "dry": 0.06,
    "dry_mesic": 0.07,
    "mesic": 0.08,
    "wet_mesic": 0.09,
    "wet": 0.10,
    "low": 0.06, "medium": 0.08, "high": 0.10,
}
_DEFAULT_POUR_FRACTION = 0.08

# garden.org "Sun Requirements" -> (min, ideal, max) lux at the plant, on a
# realistic INDOOR scale (what a home window actually delivers — not outdoor
# optimums). Measured lux still drives the watering math; this gives the default
# when unmeasured and the "too dark / too bright" warning.
SUN_TO_LUX = {
    "full_sun": (1200, 5000, 20000),
    "sun_to_part_shade": (700, 3000, 12000),
    "part_shade": (350, 1500, 7000),
    "part_to_full_shade": (150, 800, 4000),
    "full_shade": (60, 400, 2000),
}

# Legacy light categories -> representative lux (fallback when no lux/sun given).
CATEGORY_LUX = {
    "low": 1500.0, "medium": 4000.0, "bright": 12000.0, "direct": 40000.0,
}

# Plant-available water capacity (AWC) as a fraction of soil volume, by soil type.
AWC_FRACTION = {
    "standard": 0.35,
    "peat": 0.38,
    "coco": 0.40,
    "aroid": 0.28,      # chunky, fast-draining
    "cactus": 0.22,     # gritty, holds little
    "moisture": 0.45,   # water-retentive / self-watering mixes
}
_DEFAULT_AWC = 0.35

# Safety factor on the pour for pots without drainage (avoid pooling).
_NO_DRAINAGE_FACTOR = 0.8


# --------------------------------------------------------------- data classes
@dataclass
class Conditions:
    """Current environment at the plant."""
    temp_c: float
    humidity_pct: float
    lux: float | None = None


@dataclass
class SpeciesData:
    """Per-species setpoints, typically sourced from Open Plantbook.

    All fields optional; the model degrades gracefully when they are missing.
    """
    min_soil_moist: float | None = None  # %VWC
    max_soil_moist: float | None = None  # %VWC
    min_temp: float | None = None        # C
    max_temp: float | None = None        # C
    min_env_humid: float | None = None   # %
    max_env_humid: float | None = None   # %
    min_light_lux: float | None = None
    max_light_lux: float | None = None


@dataclass
class Plant:
    """Per-plant config (from plants.yaml)."""
    id: str
    name: str
    soil_volume_ml: float
    soil_type: str = "standard"
    light: str = "medium"            # legacy category, superseded by light_lux/sun
    light_lux: float | None = None   # measured lux at the plant (drives the math)
    sun: str | None = None           # garden.org Sun Requirement (key into SUN_TO_LUX)
    water_use: str = "mesic"         # key into WATER_USE_KC (garden.org Water Pref)
    growth_state: str = "auto"       # active | dormant | auto
    has_drainage: bool = True


@dataclass
class WateringRec:
    interval_days: int
    next_date: dt.date
    amount_ml: float
    daily_loss_ml: float
    explanation: str
    warnings: list[str] = field(default_factory=list)


# ------------------------------------------------------------------- physics
def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def saturation_vapor_pressure_kpa(temp_c: float) -> float:
    """Tetens equation."""
    return 0.6108 * math.exp(17.27 * temp_c / (temp_c + 237.3))


def vpd_kpa(temp_c: float, humidity_pct: float) -> float:
    """Vapour-pressure deficit — the physically correct driver of transpiration
    (combines temperature and humidity into one quantity)."""
    svp = saturation_vapor_pressure_kpa(temp_c)
    return svp * (1.0 - clamp(humidity_pct, 0.0, 100.0) / 100.0)


# Reference VPD at the baseline climate; factors are normalised against it.
VPD_REF = vpd_kpa(_BASELINE_TEMP_C, _BASELINE_RH)


def vpd_factor(temp_c: float, humidity_pct: float) -> float:
    return clamp(vpd_kpa(temp_c, humidity_pct) / VPD_REF, 0.4, 2.5)


def lux_to_factor(lux: float) -> float:
    """Transpiration multiplier from illuminance (lux), log-scaled so indoor
    values differentiate sensibly. ~1.0 at bright-indirect (~12k lux)."""
    return clamp(0.55 * math.log10(max(lux, 1.0)) - 1.05, 0.5, 1.7)


def sun_band(sun: str | None) -> tuple[float, float, float] | None:
    """(min, ideal, max) lux for a garden.org sun category, or None."""
    return SUN_TO_LUX.get((sun or "").lower())


def effective_lux(plant: Plant, conditions: "Conditions | None" = None) -> float:
    """The lux to use for a plant: measured value first, then a live sensor, then
    the garden.org sun default, then the legacy light category."""
    if plant.light_lux is not None:
        return plant.light_lux
    if conditions is not None and conditions.lux is not None:
        return conditions.lux
    band = sun_band(plant.sun)
    if band:
        return band[1]
    return CATEGORY_LUX.get((plant.light or "medium").lower(), CATEGORY_LUX["medium"])


def light_factor(light: str, lux: float | None) -> float:
    if lux is None:
        lux = CATEGORY_LUX.get((light or "medium").lower(), CATEGORY_LUX["medium"])
    return lux_to_factor(lux)


def season_factor(date: dt.date, latitude_deg: float) -> float:
    """Mild seasonal modulation from day length (annual mean ~12h)."""
    length = solar.day_length_hours(date, latitude_deg)
    return clamp(0.5 + 0.5 * (length / 12.0), 0.5, 1.4)


def growth_factor(growth_state: str, date: dt.date, latitude_deg: float) -> float:
    """active -> 1.0, dormant -> 0.4, auto -> derived from day length.

    ``auto`` is the dormancy-detection add-on: short winter days pull the plant
    toward dormancy and stretch the interval.
    """
    state = (growth_state or "auto").lower()
    if state == "active":
        return 1.0
    if state == "dormant":
        return 0.4
    # auto: ramp 0.4 (<=9h daylight) up to 1.0 (>=14h daylight)
    length = solar.day_length_hours(date, latitude_deg)
    return clamp(0.4 + 0.6 * (length - 9.0) / (14.0 - 9.0), 0.4, 1.0)


def water_use_kc(water_use: str) -> float:
    return WATER_USE_KC.get((water_use or "mesic").lower(), 1.0)


def water_use_pour_fraction(water_use: str) -> float:
    return POUR_FRACTION_BY_WATER_USE.get(
        (water_use or "mesic").lower(), _DEFAULT_POUR_FRACTION)


# --------------------------------------------------------- derived quantities
def allowed_depletion(water_use: str) -> float:
    """Management-allowed depletion fraction, from the plant's water-use type.

    OPB's soil-moisture band is a *relative* (MiFlora-style) reading and is
    nearly uniform across species, so it can't distinguish a cactus from a
    fern. The water-use type does that far better.
    """
    return MAD_BY_WATER_USE.get((water_use or "medium").lower(), 0.5)


def deplete_ml(plant: Plant, species: SpeciesData | None = None) -> float:
    """Volume of water used between waterings = available water * depletion.

    = soil_volume * available-water-capacity(soil) * allowed-depletion(type).
    Physically grounded, so pours stay in a believable range. (``species`` is
    accepted for API symmetry but no longer needed here.)
    """
    awc = AWC_FRACTION.get(plant.soil_type, _DEFAULT_AWC)
    return plant.soil_volume_ml * awc * allowed_depletion(plant.water_use)


def daily_loss_ml(plant: Plant, conditions: Conditions, date: dt.date,
                  latitude_deg: float) -> float:
    """Estimated water lost per day (evapotranspiration)."""
    f_vpd = vpd_factor(conditions.temp_c, conditions.humidity_pct)
    f_light = lux_to_factor(effective_lux(plant, conditions))
    f_season = season_factor(date, latitude_deg)
    f_growth = growth_factor(plant.growth_state, date, latitude_deg)
    kc = water_use_kc(plant.water_use)

    loss = (
        _ET_BASE_ML_PER_ML_SOIL
        * plant.soil_volume_ml
        * f_vpd
        * f_light
        * f_season
        * f_growth
        * kc
    )
    return max(loss, 0.1)  # never divide by ~zero


def pour_amount_ml(plant: Plant, species: SpeciesData | None = None) -> float:
    """A measured pour: a per-water-use fraction of soil volume (reduced for
    pots without drainage). Amount is driven by pot size + water preference."""
    amount = water_use_pour_fraction(plant.water_use) * plant.soil_volume_ml
    if not plant.has_drainage:
        amount *= _NO_DRAINAGE_FACTOR
    return amount


def _comfort_warnings(plant: Plant, species: SpeciesData | None,
                      conditions: Conditions) -> list[str]:
    warnings: list[str] = []
    band = sun_band(plant.sun)
    if band and plant.light_lux is not None:
        lo, _, hi = band
        if plant.light_lux < lo:
            warnings.append(f"dim: {plant.light_lux:.0f} lux below ideal {lo:.0f}")
        elif plant.light_lux > hi:
            warnings.append(f"bright: {plant.light_lux:.0f} lux above ideal {hi:.0f}")
    if species is not None:
        t = conditions.temp_c
        if species.min_temp is not None and t < species.min_temp:
            warnings.append(f"cold: {t:.0f}C below species min {species.min_temp:.0f}C")
        if species.max_temp is not None and t > species.max_temp:
            warnings.append(f"hot: {t:.0f}C above species max {species.max_temp:.0f}C")
    return warnings


def _describe(f_vpd: float, f_light: float, f_growth: float) -> str:
    if f_vpd >= 1.4:
        vpd_word = "warm/dry"
    elif f_vpd <= 0.7:
        vpd_word = "cool/humid"
    else:
        vpd_word = "average air"
    light_word = "bright" if f_light >= 1.2 else ("dim" if f_light <= 0.85 else "moderate")
    growth_word = "dormant" if f_growth <= 0.6 else "growing"
    return f"{vpd_word}, {light_word} light, {growth_word}"


def watering_recommendation(
    plant: Plant,
    species: SpeciesData | None,
    conditions: Conditions,
    last_watered: dt.date | None,
    today: dt.date,
    latitude_deg: float,
) -> WateringRec:
    """Compute interval, next date, and pour amount for one plant."""
    loss = daily_loss_ml(plant, conditions, today, latitude_deg)
    to_deplete = deplete_ml(plant, species)
    amount = pour_amount_ml(plant, species)

    raw_interval = to_deplete / loss
    interval = int(round(clamp(raw_interval, MIN_INTERVAL_DAYS, MAX_INTERVAL_DAYS)))

    if last_watered is None:
        # A newly-added plant surfaces a task now so it enters the cycle;
        # checking it off records the real last-watered date. (Otherwise a
        # never-watered plant anchors to "today" every run and is never due.)
        next_date = today
    else:
        next_date = last_watered + dt.timedelta(days=interval)
        # Long-overdue: water today, don't backfill missed dates.
        if next_date < today:
            next_date = today

    f_vpd = vpd_factor(conditions.temp_c, conditions.humidity_pct)
    f_light = lux_to_factor(effective_lux(plant, conditions))
    f_growth = growth_factor(plant.growth_state, today, latitude_deg)
    explanation = (
        f"{_describe(f_vpd, f_light, f_growth)} -> every {interval} days "
        f"(~{loss:.0f} ml/day lost, refill {amount:.0f} ml)"
    )

    return WateringRec(
        interval_days=interval,
        next_date=next_date,
        amount_ml=amount,
        daily_loss_ml=loss,
        explanation=explanation,
        warnings=_comfort_warnings(plant, species, conditions),
    )
