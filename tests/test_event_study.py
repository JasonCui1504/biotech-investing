"""Tests for the event-study return math (no database)."""
from app.analysis.event_study import compute_event_reaction, get_event_class, window_return

STOCK = [("2026-03-02", 10.0), ("2026-03-03", 10.0), ("2026-03-04", 8.0), ("2026-03-05", 8.5),
         ("2026-03-06", 9.0), ("2026-03-09", 9.0), ("2026-03-10", 9.5)]
BENCH = [("2026-03-02", 100.0), ("2026-03-03", 100.0), ("2026-03-04", 101.0), ("2026-03-05", 102.0),
         ("2026-03-06", 103.0), ("2026-03-09", 104.0), ("2026-03-10", 105.0)]


def test_one_day_return_uses_close_before_event():
    # Event on 03-04: baseline is the 03-03 close (10.0); day 1 is 03-04 (8.0) -> -20%.
    assert round(window_return(STOCK, "2026-03-04", 1), 4) == -0.2


def test_five_day_return_and_excess():
    result = compute_event_reaction(STOCK, BENCH, "2026-03-04")
    # day 5 = 03-10 close 9.5 -> -5%; benchmark 105/100 -> +5%; excess -10 points.
    assert round(result["ret_5d"], 4) == -0.05
    assert round(result["excess_5d"], 4) == -0.10


def test_not_enough_data_returns_none():
    assert window_return(STOCK, "2026-03-10", 5) is None
    assert window_return(STOCK, "2026-01-01", 1) is None


def test_event_class_parsing():
    assert get_event_class("[fda_approval] Approved drug X") == "fda_approval"
    assert get_event_class("plain text") is None
