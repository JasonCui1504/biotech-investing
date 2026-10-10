"""Tests for the watchlist score and table helper (no database)."""
from app.report import change_label, format_expected, markdown_table, rating_for, score_company

WEIGHTS = {"catalyst_in_window": 3, "low_ev_to_cash": 2.5, "healthy_runway": 1.5, "rnpv_above_1": 2,
           "runway_danger": -3, "large_dilution": -3, "negative_trial_change": -2, "momentum": 1}


def row(**kwargs):
    base = {"catalyst_in_window": False, "catalyst_weight": 0.0, "ev_to_cash": None, "runway_months": 30.0,
            "dilution_1y_pct": 5.0, "rnpv_base_ratio": None, "return_90d": None}
    base.update(kwargs)
    return base


def test_points_scale_with_strength():
    strong = score_company(row(catalyst_in_window=True, catalyst_weight=1.0), {}, WEIGHTS)[0]
    weak = score_company(row(catalyst_in_window=True, catalyst_weight=0.25), {}, WEIGHTS)[0]
    assert strong > weak
    cheap = score_company(row(ev_to_cash=0.0), {}, WEIGHTS)[0]
    pricey = score_company(row(ev_to_cash=2.0), {}, WEIGHTS)[0]
    assert cheap > pricey > score_company(row(ev_to_cash=4.0), {}, WEIGHTS)[0]


def test_positive_factors_add_up_with_explanations():
    score, parts = score_company(row(catalyst_in_window=True, catalyst_weight=1.0, ev_to_cash=0.0,
                                     rnpv_base_ratio=3.0, runway_months=36.0), {}, WEIGHTS)
    assert score == 3 + 2.5 + 1.5 + 2
    assert ("catalyst in window", 3.0) in parts


def test_negative_factors_scale():
    score, _ = score_company(row(runway_months=6.0, dilution_1y_pct=60.0), {"negative_trial_change": True}, WEIGHTS)
    assert score == -1.5 - 3 - 2
    mild, _ = score_company(row(runway_months=6.0, dilution_1y_pct=20.0), {}, WEIGHTS)
    assert mild > score


def test_cash_flow_positive_company_gets_no_runway_points_but_no_penalty():
    score, parts = score_company(row(runway_months=float("nan")), {}, WEIGHTS)
    assert score == 0 and parts == []


def test_rating_and_change_label():
    thresholds = {"buy": 4.0, "sell": 0.0}
    assert [rating_for(x, thresholds) for x in (5, 4, 2, 0, -1)] == ["BUY", "BUY", "HOLD", "SELL", "SELL"]
    assert change_label("BUY", None) == "new"
    assert change_label("BUY", "BUY") == "no change"
    assert change_label("BUY", "HOLD") == "UPGRADE (was HOLD)"
    assert change_label("SELL", "HOLD") == "DOWNGRADE (was HOLD)"


def test_markdown_table():
    assert markdown_table(["A", "B"], [[1, "x|y"]]).splitlines()[2] == "| 1 | x/y |"
    assert markdown_table(["A"], []) == "_none_"


def test_format_expected_shows_period_not_a_fake_date():
    assert format_expected("2026-01-01", "year") == "2026"
    assert format_expected("2026-07-01", "half") == "2H 2026"
    assert format_expected("2026-10-01", "quarter") == "Q4 2026"
    assert format_expected("2026-03-01", "month") == "2026-03"
    assert format_expected("2026-03-15", "exact") == "2026-03-15"


def test_brief_to_html_renders_tables_and_headings_with_inline_styles():
    from app.report import brief_to_html
    out = brief_to_html("# Title\n\n## 1. Part\n\n| A | B |\n|---|---|\n| 1 | <x> |\n\n- **T** ok\n")
    assert "<table style=" in out and "<h2 style=" in out and "<li style=" in out
    assert "&lt;x&gt;" in out and "<x>" not in out
