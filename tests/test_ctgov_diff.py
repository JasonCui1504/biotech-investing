"""Tests for trial change detection and sponsor-name matching (no network)."""
from app.ingest.ctgov import diff_snapshots, match_sponsor, normalize_name, parse_loose_date

BASE = {"nct_id": "NCT05012345", "status": "RECRUITING", "enrollment": 100,
        "primary_endpoint": "ORR", "primary_completion_date": "2026-12"}


def changed(**kwargs):
    new = dict(BASE)
    new.update(kwargs)
    return diff_snapshots(BASE, new)


def test_no_change_gives_empty_list():
    assert diff_snapshots(BASE, dict(BASE)) == []


def test_terminated_is_materiality_5():
    result = changed(status="TERMINATED")
    assert result[0][1] == 5


def test_completed_is_materiality_4():
    assert changed(status="COMPLETED")[0][1] == 4


def test_delay_over_three_months_is_flagged():
    result = changed(primary_completion_date="2027-06")
    assert result[0][1] == 4
    assert "2026-12 -> 2027-06" in result[0][0]


def test_small_delay_is_ignored_and_earlier_is_3():
    assert changed(primary_completion_date="2027-01") == []
    assert changed(primary_completion_date="2026-06")[0][1] == 3


def test_enrollment_threshold():
    assert changed(enrollment=110) == []
    assert changed(enrollment=130)[0][1] == 3


def test_endpoint_change_is_4():
    assert changed(primary_endpoint="PFS")[0][1] == 4


def test_name_matching():
    assert normalize_name("Summit Therapeutics Inc.") == "summit therapeutics"
    assert match_sponsor("Summit Therapeutics, Inc.", "Summit Therapeutics Inc.") == "exact"
    assert match_sponsor("Summit Therapeutic Inc", "Summit Therapeutics Inc.") == "fuzzy"
    assert match_sponsor("Pfizer", "Summit Therapeutics Inc.") is None


def test_loose_dates():
    assert str(parse_loose_date("2026-12")) == "2026-12-01"
    assert parse_loose_date("garbage") is None
