"""Tests for the pure screen calculations (no database needed)."""
from app.analysis.screens import (compute_burn, compute_enterprise_value, compute_pct_change,
                                  compute_runway_months, runway_flag)

CONFIG = {"screens": {"min_runway_months_ok": 18, "runway_warning_months": 12}}


def test_burn_averages_recent_quarters_as_positive_number():
    assert compute_burn([-30, -30, -30, -30, -999]) == 30  # only the latest 4 count


def test_burn_none_when_cash_flow_positive_or_too_little_data():
    assert compute_burn([10, 5, -3]) is None
    assert compute_burn([-30]) is None


def test_runway_months():
    # $300M cash, $50M/quarter burn -> $16.67M/month -> 18 months.
    assert round(compute_runway_months(300, 50), 1) == 18.0
    assert compute_runway_months(300, None) is None


def test_enterprise_value():
    assert compute_enterprise_value(500, 20, 300) == 220
    assert compute_enterprise_value(500, None, 300) == 200
    assert compute_enterprise_value(None, 0, 300) is None


def test_pct_change():
    assert compute_pct_change(100, 150) == 50
    assert compute_pct_change(0, 150) is None


def test_runway_flags():
    assert runway_flag(24, CONFIG) == "OK"
    assert runway_flag(14, CONFIG) == "WATCH"
    assert runway_flag(8, CONFIG) == "DANGER"
    assert runway_flag(None, CONFIG) == "OK"
