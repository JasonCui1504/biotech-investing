"""Tests for classifier output cleaning and RSS helpers (no network)."""
from app.analysis.classify import validate_item
from app.ingest.news import clean_snippet, pick_column

KNOWN = {"SMMT": "Summit Therapeutics"}


def test_validate_item_cleans_bad_values():
    item = {"id": 1, "ticker": "FAKE", "event_type": "made_up", "materiality": 9}
    result = validate_item(item, KNOWN)
    assert result["ticker"] is None
    assert result["event_type"] == "other"
    assert result["materiality"] == 5


def test_validate_item_keeps_good_values():
    item = {"ticker": "SMMT", "event_type": "fda_approval", "materiality": "4",
            "one_line_summary": "Approved"}
    result = validate_item(item, KNOWN)
    assert (result["ticker"], result["event_type"], result["materiality"]) == ("SMMT", "fda_approval", 4)


def test_bad_materiality_defaults_to_1():
    assert validate_item({"materiality": None}, KNOWN)["materiality"] == 1


def test_snippet_and_column_helpers():
    assert clean_snippet("<p>Hello <b>there</b></p>") == "Hello there"
    assert len(clean_snippet("x" * 2000)) == 500
    assert pick_column(["link", "headline"], ["url", "link"]) == "link"
    assert pick_column(["a"], ["url"]) is None
