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

# Per-plant water-use coefficient (transpiration intensity). This captures the
# big difference OPB's soil-moisture band does NOT: a cactus and a fern in
# identical pots dry at very different rates.
WATER_USE_KC = {
    "low": 0.3,      # cacti, succulents, sansevieria, ZZ (CAM — very low transpiration)
    "medium": 1.0,   # pothos, monstera, most foliage
    "high": 1.45,    # ferns, calathea, fittonia, thirsty growers
}

# Management-allowed depletion (MAD) by water-use type: how much of the available
# water we let the plant use before rewatering. Drought-tolerant plants are
# watered deeply then allowed to dry right out; moisture-lovers are kept evenly
# damp. This drives interval far more meaningfully than OPB's (coarse) band.
MAD_BY_WATER_USE = {
    "low": 0.9,      # let it get bone dry
    "medium": 0.5,
    "high": 0.35,    # keep it damp
}

# Light -> transpiration multiplier is computed from lux (measurable with a
# phone light-meter app). Categories map to representative lux, used only when
# no measured value is given.
CATEGORY_LUX = {
    "low": 1500.0,      # a few feet from a window / north-facing
    "medium": 4000.0,   # bright room, not right by glass
    "bright": 12000.0,  # beside a bright window (bright indirect)
    "direct": 40000.0,  # direct sun falling on the leaves
}

# Plant-available water capacity (AWC) as a fraction of soil volume, by soil
# type: how much of the pot's volume is water the roots can actually use between
# saturation and wilting. Peat/coir hold a lot; gritty cactus mixes little.
AWC_FRACTION = {
    "standard": 0.35,
    "peat": 0.38,
    "coco": 0.40,
    "aroid": 0.28,      # chunky, fast-draining
    "cactus": 0.22,     # gritty, holds little
    "moisture": 0.45,   # water-retentive / self-watering mixes
}
_DEFAULT_AWC = 0.35

# Cap a single pour to this fraction of soil volume — keeps us in "measured
# pour" territory rather than soak-until-runoff. Lowered after real pours were
# overflowing to the drainage pan (esp. large pots like the fiddle leaf).
_POUR_CAP_FRACTION = 0.08

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
    light: str = "medium"            # category key, or a numeric lux via light_lux
    light_lux: float | None = None
    water_use: str = "medium"        # key into WATER_USE_KC
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
    return WATER_USE_KC.get((water_use or "medium").lower(), 1.0)


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
    f_light = light_factor(plant.light, plant.light_lux or conditions.lux)
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


def pour_amount_ml(plant: Plant, species: SpeciesData | None) -> float:
    amount = deplete_ml(plant, species)
    # Keep it a measured pour, not a flood.
    amount = min(amount, _POUR_CAP_FRACTION * plant.soil_volume_ml)
    if not plant.has_drainage:
        amount *= _NO_DRAINAGE_FACTOR
    return amount


def _comfort_warnings(species: SpeciesData | None, conditions: Conditions) -> list[str]:
    warnings: list[str] = []
    if species is None:
        return warnings
    t = conditions.temp_c
    if species.min_temp is not None and t < species.min_temp:
        warnings.append(f"cold: {t:.0f}C below species min {species.min_temp:.0f}C")
    if species.max_temp is not None and t > species.max_temp:
        warnings.append(f"hot: {t:.0f}C above species max {species.max_temp:.0f}C")
    h = conditions.humidity_pct
    if species.min_env_humid is not None and h < species.min_env_humid:
        warnings.append(f"dry air: {h:.0f}% below species min {species.min_env_humid:.0f}%")
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

    anchor = last_watered or today
    next_date = anchor + dt.timedelta(days=interval)
    # Never schedule in the past (e.g. long-overdue plants) — water today.
    if next_date < today:
        next_date = today

    f_vpd = vpd_factor(conditions.temp_c, conditions.humidity_pct)
    f_light = light_factor(plant.light, plant.light_lux or conditions.lux)
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
        warnings=_comfort_warnings(species, conditions),
    )
