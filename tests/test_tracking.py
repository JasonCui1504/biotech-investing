"""Tests for performance math and position sizing (no database)."""
from app.tracking.performance import compute_rec_result, position_size, price_on_or_before, summarize_results


def test_excess_return_over_benchmark():
    stock, bench, excess = compute_rec_result(10.0, 12.0, 100.0, 105.0)
    assert round(stock, 4) == 0.2 and round(bench, 4) == 0.05 and round(excess, 4) == 0.15


def test_missing_data_gives_none():
    assert compute_rec_result(None, 12.0, 100.0, 105.0) == (None, None, None)
    assert compute_rec_result(10.0, 12.0, None, None)[2] is None


def test_summary_statistics():
    results = [{"ret": 0.2, "excess": 0.1}, {"ret": -0.3, "excess": -0.35}, {"ret": None, "excess": None}]
    stats = summarize_results(results)
    assert stats["n"] == 2 and stats["win_rate"] == 0.5
    assert round(stats["avg_excess"], 3) == -0.125 and stats["worst_loss"] == -0.3


def test_position_size_is_capped():
    assert position_size(100000, 4, 5) == 5000      # cap 5% beats 25% equal weight
    assert position_size(100000, 40, 5) == 2500     # equal weight is below the cap
    assert position_size(100000, 0, 5) == 0.0


def test_price_lookup():
    prices = [("2026-01-02", 1.0), ("2026-01-05", 2.0)]
    assert price_on_or_before(prices, "2026-01-04") == 1.0
    assert price_on_or_before(prices, "2025-12-31") is None
