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


def test_pour_replaces_what_was_lost():
    """The pour must match deplete_ml: the interval is defined as the time to
    lose that much, so pouring less leaves the soil drier every cycle."""
    plant = make_plant(soil_volume_ml=5000.0, water_use="mesic")
    assert wm.pour_amount_ml(plant) == pytest.approx(
        wm.deplete_ml(plant) * wm._RUNOFF_ALLOWANCE)


def test_drought_tolerant_pours_more_not_less():
    # Watered rarely, but thoroughly: a plant allowed to dry right out has lost
    # more by the time it's due, so it needs more putting back.
    dry = make_plant(water_use="dry")
    wet = make_plant(water_use="wet")
    assert wm.pour_amount_ml(dry) > wm.pour_amount_ml(wet)


def test_no_drainage_reduces_pour():
    drained = wm.pour_amount_ml(make_plant(has_drainage=True))
    undrained = wm.pour_amount_ml(make_plant(has_drainage=False))
    assert undrained < drained
    assert undrained == pytest.approx(
        drained * wm._NO_DRAINAGE_FACTOR / wm._RUNOFF_ALLOWANCE)


def test_pot_size_changes_interval():
    """Pot volume used to cancel out of deplete/loss entirely, so a 250 ml pot
    and a 25 L pot came out identical. Bigger pots must last longer."""
    cond = wm.Conditions(temp_c=21, humidity_pct=50)
    small = _interval(make_plant(soil_volume_ml=250.0), cond)
    large = _interval(make_plant(soil_volume_ml=25000.0), cond)
    assert large > small * 2


def test_interval_scales_sublinearly_with_volume():
    # loss ~ V^0.8, storage ~ V, so interval ~ V^0.2: 8x the volume is about
    # 1.5x the interval -- bigger pots last longer, but not proportionally.
    cond = wm.Conditions(temp_c=21, humidity_pct=50)
    base = _interval(make_plant(soil_volume_ml=1000.0), cond)
    eight = _interval(make_plant(soil_volume_ml=8000.0), cond)
    expected = 8.0 ** (1.0 - wm._ET_VOLUME_EXPONENT)
    assert eight == pytest.approx(base * expected, rel=0.25)


def test_five_level_water_use_interval_monotonic():
    cond = wm.Conditions(temp_c=22, humidity_pct=50)
    ivs = [_interval(make_plant(water_use=w), cond)
           for w in ["dry", "dry_mesic", "mesic", "wet_mesic", "wet"]]
    assert ivs == sorted(ivs, reverse=True)  # drier prefs -> longer intervals


def test_legacy_water_use_aliases():
    assert wm.allowed_depletion("low") == wm.allowed_depletion("dry")
    assert wm.allowed_depletion("medium") == wm.allowed_depletion("mesic")
    assert wm.allowed_depletion("high") == wm.allowed_depletion("wet")


def test_leaf_type_drives_transpiration_not_water_preference():
    """garden.org gives ZZ and pothos the same water preference, but a CAM
    succulent drinks far slower than a thin-leaved aroid. Leaf type is what
    separates them."""
    cond = wm.Conditions(temp_c=21, humidity_pct=50)
    zz = make_plant(water_use="mesic", leaf_type="succulent")
    pothos = make_plant(water_use="mesic", leaf_type="normal")
    assert _interval(zz, cond) > _interval(pothos, cond) * 2


def test_leaf_kc_ordering_and_default():
    assert (wm.leaf_kc("succulent") < wm.leaf_kc("waxy")
            < wm.leaf_kc("normal") < wm.leaf_kc("thin"))
    assert wm.leaf_kc(None) == wm.leaf_kc("normal")
    assert wm.leaf_kc("nonsense") == wm.leaf_kc("normal")


# ------------------------------------------------------- light by placement
def test_estimate_lux_south_window_is_brightest():
    at_glass = {"distance": "in_window"}
    luxes = [wm.estimate_lux(w, **at_glass) for w in ["south", "west", "east", "north"]]
    assert luxes == sorted(luxes, reverse=True)


def test_estimate_lux_falls_off_with_distance():
    luxes = [wm.estimate_lux("west", d)
             for d in ["in_window", "near", "mid", "far"]]
    assert luxes == sorted(luxes, reverse=True)


def test_filtered_light_reduces_estimate():
    assert wm.estimate_lux("south", "near", filtered=True) < wm.estimate_lux("south", "near")


def test_no_window_ignores_distance():
    # Distance from a window is meaningless when there isn't one.
    assert wm.estimate_lux("none", "in_window") == wm.estimate_lux("none", "far")


@pytest.mark.parametrize("window,distance,sun", [
    ("south", "in_window", "full_sun"),
    ("west",  "in_window", "sun_to_part_shade"),
    ("south", "near",      "sun_to_part_shade"),
    ("west",  "near",      "part_shade"),
    ("east",  "near",      "part_shade"),
    ("north", "in_window", "part_shade"),
    ("west",  "mid",       "part_to_full_shade"),
    ("north", "near",      "part_to_full_shade"),
    ("south", "far",       "part_to_full_shade"),
    ("north", "mid",       "full_shade"),
    ("east",  "far",       "full_shade"),
    ("none",  "near",      "full_shade"),
])
def test_placement_lands_in_its_intended_sun_band(window, distance, sun):
    """The placement scale is anchored to SUN_TO_LUX: a spot that can support a
    given sun category must actually land inside that category's band. This is
    the calibration -- if someone retunes one table without the other, this
    fails."""
    lux = wm.estimate_lux(window, distance)
    lo, _, hi = wm.SUN_TO_LUX[sun]
    assert lo <= lux <= hi, f"{window}/{distance} = {lux:.0f} outside {sun} {lo}-{hi}"


def test_seasonal_intensity_averages_one_over_the_year():
    # The placement table is calibrated as a year-round average, so the seasonal
    # factor must swing around it, not shift it.
    year = dt.date(2026, 1, 1)
    vals = [solar.solar_intensity_factor(year + dt.timedelta(days=i), LAT)
            for i in range(365)]
    assert sum(vals) / len(vals) == pytest.approx(1.0, abs=0.02)


def test_winter_light_weaker_than_summer():
    assert (solar.solar_intensity_factor(WINTER, LAT)
            < 1.0 < solar.solar_intensity_factor(SUMMER, LAT))


def test_equator_has_little_seasonal_swing():
    lo = solar.solar_intensity_factor(WINTER, 0.0)
    hi = solar.solar_intensity_factor(SUMMER, 0.0)
    assert abs(hi - lo) < 0.15


def test_seasonal_factor_applies_to_placement_lux():
    plant = make_plant(window="south", distance="near")
    summer = wm.plant_lux(plant, SUMMER, LAT)
    winter = wm.plant_lux(plant, WINTER, LAT)
    annual = wm.plant_lux(plant)  # no date -> unadjusted baseline
    assert winter < annual < summer


def test_seasonal_factor_skips_measured_lux():
    # A measured value is a snapshot from one season; don't re-scale it.
    plant = make_plant(light_lux=1500)
    assert wm.plant_lux(plant, WINTER, LAT) == 1500
    assert wm.plant_lux(plant, SUMMER, LAT) == 1500


def test_winter_lengthens_interval_via_light():
    cond = wm.Conditions(temp_c=22, humidity_pct=50)
    plant = make_plant(window="south", distance="near", growth_state="active")
    summer = wm.watering_recommendation(
        plant, None, cond, SUMMER, SUMMER, LAT).interval_days
    winter = wm.watering_recommendation(
        plant, None, cond, WINTER, WINTER, LAT).interval_days
    assert winter > summer


def test_estimate_lux_never_goes_pitch_black():
    assert wm.estimate_lux("north", "far", filtered=True) >= wm._MIN_ESTIMATED_LUX


def test_unset_window_returns_none():
    assert wm.estimate_lux(None) is None
    assert wm.estimate_lux("") is None


def test_placement_overrides_stale_measured_lux():
    # A stale one-off meter reading must not beat where the plant actually sits.
    plant = make_plant(light_lux=41000, window="north", distance="far")
    assert wm.plant_lux(plant) == wm.estimate_lux("north", "far")
    assert wm.plant_lux(plant) < 1000


def test_measured_lux_still_used_when_no_placement():
    assert wm.plant_lux(make_plant(light_lux=1234)) == 1234


def test_placement_drives_interval():
    cond = wm.Conditions(temp_c=22, humidity_pct=50)
    sunny = _interval(make_plant(window="south", distance="in_window"), cond)
    dark = _interval(make_plant(window="none"), cond)
    assert sunny < dark


def test_placement_triggers_dim_warning():
    cond = wm.Conditions(temp_c=22, humidity_pct=50)
    plant = make_plant(sun="part_shade", window="none")  # band min 350
    rec = wm.watering_recommendation(plant, None, cond, SUMMER, SUMMER, LAT)
    assert any("dim" in w for w in rec.warnings)


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


def test_evaporation_floor_bounds_winter_loss():
    # Soil keeps evaporating when the plant is dormant in dim light, so loss
    # must not multiply down toward zero.
    plant = make_plant(water_use="dry", soil_type="cactus",
                       growth_state="dormant", window="north", distance="far")
    cond = wm.Conditions(temp_c=16, humidity_pct=80)  # cold, damp, minimal demand
    loss = wm.daily_loss_ml(plant, cond, WINTER, LAT)
    baseline = (wm._ET_BASE_ML_PER_ML_SOIL
                * wm._evaporating_volume_ml(plant.soil_volume_ml)
                * wm.leaf_kc(plant.leaf_type))
    assert loss == pytest.approx(wm._EVAP_FLOOR_FRACTION * baseline)


def test_floor_does_not_bind_in_summer():
    # A thirsty plant in good light must be driven by the model, not the floor.
    plant = make_plant(water_use="wet", window="south", distance="in_window")
    cond = wm.Conditions(temp_c=26, humidity_pct=35)
    loss = wm.daily_loss_ml(plant, cond, SUMMER, LAT)
    baseline = (wm._ET_BASE_ML_PER_ML_SOIL
                * wm._evaporating_volume_ml(plant.soil_volume_ml)
                * wm.leaf_kc(plant.leaf_type))
    assert loss > wm._EVAP_FLOOR_FRACTION * baseline


def test_seasonal_swing_is_plausible_not_extreme():
    """Winter should stretch the interval roughly 2x, not 5x.

    Three terms key off day length (shorter days, weaker sun, dormancy); this
    pins the combined effect to something horticulturally sane.
    """
    cond = wm.Conditions(temp_c=21, humidity_pct=50)
    plant = make_plant(water_use="mesic", window="east", distance="near")
    summer = wm.watering_recommendation(plant, None, cond, SUMMER, SUMMER, LAT).interval_days
    winter = wm.watering_recommendation(plant, None, cond, WINTER, WINTER, LAT).interval_days
    assert 1.4 <= winter / summer <= 3.0


def test_autumn_and_winter_are_distinguishable():
    # The old 30-day cap (and later an over-tight floor) collapsed these.
    cond = wm.Conditions(temp_c=21, humidity_pct=50)
    plant = make_plant(water_use="dry_mesic", window="north", distance="near")
    sep = wm.watering_recommendation(
        plant, None, cond, dt.date(2026, 9, 21), dt.date(2026, 9, 21), LAT).interval_days
    dec = wm.watering_recommendation(plant, None, cond, WINTER, WINTER, LAT).interval_days
    assert dec > sep


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
