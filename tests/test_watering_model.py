"""Unit tests for the watering model.

Run with:  ./venv/bin/python -m pytest tests/ -q
"""
import datetime as dt
import math

import pytest

import solar
import watering_model as wm

LAT = 40.0
SUMMER = dt.date(2026, 6, 21)
WINTER = dt.date(2026, 12, 21)


def make_plant(**kw):
    base = dict(id="p", name="P", soil_volume_ml=2000.0)
    base.update(kw)
    return wm.Plant(**base)


def monstera_species():
    # Real Open Plantbook values for monstera deliciosa.
    return wm.SpeciesData(
        min_soil_moist=15, max_soil_moist=60,
        min_temp=12, max_temp=32,
        min_env_humid=30, max_env_humid=85,
        min_light_lux=800, max_light_lux=15000,
    )


# ------------------------------------------------------------------ VPD math
def test_saturation_vapor_pressure_known_value():
    # SVP at 20 C is ~2.338 kPa.
    assert wm.saturation_vapor_pressure_kpa(20.0) == pytest.approx(2.338, abs=0.02)


def test_vpd_zero_at_full_saturation():
    assert wm.vpd_kpa(25.0, 100.0) == pytest.approx(0.0, abs=1e-9)


def test_vpd_increases_with_temp_and_dryness():
    assert wm.vpd_kpa(30, 40) > wm.vpd_kpa(20, 40)   # hotter -> higher
    assert wm.vpd_kpa(25, 20) > wm.vpd_kpa(25, 80)   # drier -> higher


def test_vpd_factor_baseline_is_one():
    assert wm.vpd_factor(21.0, 50.0) == pytest.approx(1.0, abs=1e-6)


# ------------------------------------------------------------------ amount
def test_amount_is_volume_times_awc_times_depletion():
    plant = make_plant(soil_type="standard", soil_volume_ml=2000.0, water_use="medium")
    # AWC(standard)=0.35; MAD(medium)=0.5
    expected = 2000.0 * 0.35 * 0.5
    assert wm.deplete_ml(plant) == pytest.approx(expected)


def test_amount_uses_awc_and_water_use():
    plant = make_plant(soil_type="cactus", soil_volume_ml=1000.0, water_use="low")
    expected = 1000.0 * wm.AWC_FRACTION["cactus"] * wm.MAD_BY_WATER_USE["low"]
    assert wm.deplete_ml(plant) == pytest.approx(expected)


def test_drought_tolerant_depletes_more_than_moisture_lover():
    thirsty = make_plant(water_use="high")   # kept damp -> small depletion
    succulent = make_plant(water_use="low")  # dries right out -> large depletion
    assert wm.deplete_ml(succulent) > wm.deplete_ml(thirsty)


def test_pour_is_fraction_of_volume_by_water_use():
    # mesic -> 8% of soil volume.
    plant = make_plant(soil_volume_ml=5000.0, water_use="mesic")
    assert wm.pour_amount_ml(plant) == pytest.approx(0.08 * 5000.0)


def test_wetter_preference_pours_more():
    dry = make_plant(water_use="dry")
    wet = make_plant(water_use="wet")
    assert wm.pour_amount_ml(wet) > wm.pour_amount_ml(dry)


def test_no_drainage_reduces_pour():
    drained = wm.pour_amount_ml(make_plant(has_drainage=True))
    undrained = wm.pour_amount_ml(make_plant(has_drainage=False))
    assert undrained < drained
    assert undrained == pytest.approx(drained * wm._NO_DRAINAGE_FACTOR)


def test_five_level_water_use_interval_monotonic():
    cond = wm.Conditions(temp_c=22, humidity_pct=50)
    ivs = [_interval(make_plant(water_use=w), cond)
           for w in ["dry", "dry_mesic", "mesic", "wet_mesic", "wet"]]
    assert ivs == sorted(ivs, reverse=True)  # drier prefs -> longer intervals


def test_legacy_water_use_aliases():
    assert wm.water_use_kc("low") == wm.water_use_kc("dry")
    assert wm.water_use_kc("medium") == wm.water_use_kc("mesic")
    assert wm.water_use_kc("high") == wm.water_use_kc("wet")


def test_sun_band_flags_too_dim():
    cond = wm.Conditions(temp_c=22, humidity_pct=50)
    plant = make_plant(sun="part_shade", light_lux=100)  # band min 350
    rec = wm.watering_recommendation(plant, None, cond, SUMMER, SUMMER, LAT)
    assert any("dim" in w for w in rec.warnings)


# --------------------------------------------------------- interval behaviour
def _interval(plant, conditions, date=SUMMER, species=None):
    return wm.watering_recommendation(
        plant, species, conditions, last_watered=date,
        today=date, latitude_deg=LAT,
    ).interval_days


def test_hotter_drier_shortens_interval():
    plant = make_plant()
    cool = wm.Conditions(temp_c=18, humidity_pct=70)
    hot = wm.Conditions(temp_c=30, humidity_pct=25)
    assert _interval(plant, hot) < _interval(plant, cool)


def test_brighter_light_shortens_interval():
    cond = wm.Conditions(temp_c=22, humidity_pct=50)
    dim = _interval(make_plant(light="low"), cond)
    bright = _interval(make_plant(light="direct"), cond)
    assert bright < dim


def test_lux_to_factor_monotonic_and_bounded():
    assert wm.lux_to_factor(500) < wm.lux_to_factor(5000) < wm.lux_to_factor(50000)
    assert 0.5 <= wm.lux_to_factor(1) <= 1.7
    assert 0.5 <= wm.lux_to_factor(200000) <= 1.7


def test_measured_lux_overrides_category():
    # A plant tagged 'direct' but measured at a dim 800 lux should transpire
    # like a dim spot, not a sunny one.
    cond = wm.Conditions(temp_c=22, humidity_pct=50)
    tagged_direct = _interval(make_plant(light="direct"), cond)
    measured_dim = _interval(make_plant(light="direct", light_lux=800), cond)
    assert measured_dim > tagged_direct


def test_low_water_use_lengthens_interval():
    cond = wm.Conditions(temp_c=22, humidity_pct=50)
    succulent = _interval(make_plant(water_use="low"), cond)
    fern = _interval(make_plant(water_use="high"), cond)
    assert succulent > fern


def test_dormant_lengthens_interval():
    cond = wm.Conditions(temp_c=22, humidity_pct=50)
    active = _interval(make_plant(growth_state="active"), cond)
    dormant = _interval(make_plant(growth_state="dormant"), cond)
    assert dormant > active


def test_auto_dormancy_stretches_in_winter():
    cond = wm.Conditions(temp_c=22, humidity_pct=50)
    plant = make_plant(growth_state="auto")
    assert _interval(plant, cond, date=WINTER) > _interval(plant, cond, date=SUMMER)


def test_interval_within_guard_rails():
    plant = make_plant()
    extreme_hot = wm.Conditions(temp_c=40, humidity_pct=5)
    extreme_cool = wm.Conditions(temp_c=10, humidity_pct=95)
    assert wm.MIN_INTERVAL_DAYS <= _interval(plant, extreme_hot) <= wm.MAX_INTERVAL_DAYS
    assert wm.MIN_INTERVAL_DAYS <= _interval(plant, extreme_cool) <= wm.MAX_INTERVAL_DAYS


def test_representative_plant_dries_in_about_nine_days():
    # medium water use, standard soil, active growth, baseline climate.
    # ET tuned to a ~8-9 day baseline (split of original and longer values).
    plant = make_plant(water_use="medium", soil_type="standard", growth_state="active")
    cond = wm.Conditions(temp_c=21, humidity_pct=50)
    interval = _interval(plant, cond, date=dt.date(2026, 3, 20))
    assert 7 <= interval <= 11


def test_never_watered_plant_is_due_today():
    plant = make_plant()
    cond = wm.Conditions(temp_c=22, humidity_pct=50)
    today = dt.date(2026, 7, 25)
    rec = wm.watering_recommendation(
        plant, None, cond, last_watered=None, today=today, latitude_deg=LAT)
    assert rec.next_date == today  # surfaces immediately, doesn't slide forward


def test_overdue_plant_is_scheduled_today_not_past():
    plant = make_plant()
    cond = wm.Conditions(temp_c=22, humidity_pct=50)
    today = dt.date(2026, 7, 8)
    rec = wm.watering_recommendation(
        plant, monstera_species(), cond,
        last_watered=dt.date(2026, 1, 1),  # long overdue
        today=today, latitude_deg=LAT,
    )
    assert rec.next_date >= today


# ------------------------------------------------------------------ warnings
def test_cold_triggers_warning():
    cond = wm.Conditions(temp_c=5, humidity_pct=50)
    rec = wm.watering_recommendation(
        make_plant(), monstera_species(), cond,
        last_watered=SUMMER, today=SUMMER, latitude_deg=LAT,
    )
    assert any("cold" in w for w in rec.warnings)


# --------------------------------------------------------------------- solar
def test_day_length_summer_longer_than_winter():
    assert solar.day_length_hours(SUMMER, LAT) > solar.day_length_hours(WINTER, LAT)


def test_day_length_symmetry_around_solstice():
    # Days equidistant from the summer solstice have near-equal length.
    before = solar.day_length_hours(dt.date(2026, 5, 21), LAT)
    after = solar.day_length_hours(dt.date(2026, 7, 21), LAT)
    assert abs(before - after) < 0.3


def test_equator_day_length_near_twelve():
    assert solar.day_length_hours(SUMMER, 0.0) == pytest.approx(12.0, abs=0.2)
