"""Tests for parse_timing_phrase and 10-K section finding (no network)."""
from app.analysis.extract import find_business_section, guess_catalyst_type, parse_timing_phrase


def test_quarters():
    assert parse_timing_phrase("Q1 2027") == ("2027-01-01", "quarter")
    assert parse_timing_phrase("topline data in Q4 of 2026") == ("2026-10-01", "quarter")


def test_halves():
    assert parse_timing_phrase("1H 2027") == ("2027-01-01", "half")
    assert parse_timing_phrase("2H 2026") == ("2026-07-01", "half")
    assert parse_timing_phrase("second half of 2027") == ("2027-07-01", "half")


def test_mid_year_and_year_end():
    assert parse_timing_phrase("mid-2027") == ("2027-07-01", "half")
    assert parse_timing_phrase("by year-end 2026") == ("2026-12-31", "year")
    assert parse_timing_phrase("end of 2026") == ("2026-12-31", "year")


def test_months_and_exact_dates():
    assert parse_timing_phrase("March 2027") == ("2027-03-01", "month")
    assert parse_timing_phrase("PDUFA date of March 15, 2027") == ("2027-03-15", "exact")
    assert parse_timing_phrase("2027-03-15") == ("2027-03-15", "exact")


def test_unparseable_and_empty():
    assert parse_timing_phrase("soon") == (None, None)
    assert parse_timing_phrase(None) == (None, None)


def test_business_section_skips_table_of_contents():
    toc = "Item 1. Business 3 Item 1A. Risk Factors 20 "
    body = "Item 1. Business " + ("We develop drugs. " * 600) + "Item 1A. Risk Factors more text"
    section = find_business_section(toc + body)
    assert section.startswith("Item 1. Business We develop")
    assert len(section) > 5000


def test_catalyst_type_guess():
    assert guess_catalyst_type("PDUFA date") == "pdufa"
    assert guess_catalyst_type("initiate Phase 3") == "phase_start"
    assert guess_catalyst_type("topline results") == "topline_data"
