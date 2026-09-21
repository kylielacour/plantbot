"""Tests for climate sampling.

Run with:  ./venv/bin/python -m pytest tests/ -q
"""
import datetime as dt

import pytest

from climate import time_weighted_mean

BASE = dt.datetime(2026, 9, 20, 0, 0, tzinfo=dt.timezone.utc)


def at(hours: float, value: float):
    return (BASE + dt.timedelta(hours=hours), value)


def test_empty_series_returns_none():
    assert time_weighted_mean([], BASE) is None


def test_single_reading_holds_for_the_whole_window():
    assert time_weighted_mean([at(0, 61.0)], BASE + dt.timedelta(hours=24)) == 61.0


def test_equal_durations_match_plain_mean():
    pts = [at(0, 60.0), at(12, 70.0)]
    assert time_weighted_mean(pts, BASE + dt.timedelta(hours=24)) == pytest.approx(65.0)


def test_short_spike_barely_moves_the_mean():
    """The whole point: a shower spike lasting an hour must not count as much
    as the 23 hours either side of it."""
    pts = [at(0, 60.0), at(13, 84.0), at(14, 60.0)]
    mean = time_weighted_mean(pts, BASE + dt.timedelta(hours=24))
    assert mean == pytest.approx(61.0, abs=0.1)


def test_dense_samples_during_spike_do_not_dominate():
    """HA emits a row per change, so a volatile hour produces many rows. A
    naive average over rows would be dragged toward the spike; the
    time-weighted one must not be."""
    spike = [at(13 + i * 0.1, 84.0) for i in range(10)]  # 10 rows in one hour
    pts = [at(0, 60.0)] + spike + [at(14, 60.0)]
    naive = sum(v for _, v in pts) / len(pts)
    weighted = time_weighted_mean(pts, BASE + dt.timedelta(hours=24))
    assert naive > 70.0          # rows are dominated by the spike
    assert weighted < 62.0       # real elapsed time is not
