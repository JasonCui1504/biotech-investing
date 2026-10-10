"""Tests for the watchlist score and table helper (no database)."""
from app.report import format_expected, markdown_table, score_company

WEIGHTS = {"catalyst_in_window": 2, "low_ev_to_cash": 2, "healthy_runway": 1, "rnpv_above_1": 2,
           "runway_danger": -3, "large_dilution": -2, "negative_trial_change": -2}


def row(**kwargs):
    base = {"catalyst_in_window": False, "ev_cash_flag": False, "runway_flag": "OK", "runway_months": 30.0,
            "dilution_1y_pct": 5.0, "rnpv_base_ratio": None}
    base.update(kwargs)
    return base


def test_positive_factors_add_up_with_explanations():
    score, parts = score_company(row(catalyst_in_window=True, ev_cash_flag=True, rnpv_base_ratio=1.5), {}, WEIGHTS)
    assert score == 2 + 2 + 1 + 2
    assert ("catalyst in window", 2) in parts


def test_negative_factors():
    score, _ = score_company(row(runway_flag="DANGER", runway_months=6.0, dilution_1y_pct=30.0),
                             {"negative_trial_change": True}, WEIGHTS)
    assert score == -3 - 2 - 2


def test_cash_flow_positive_company_gets_no_runway_points_but_no_penalty():
    score, parts = score_company(row(runway_months=float("nan")), {}, WEIGHTS)
    assert score == 0 and parts == []


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
