"""Tests for memo helpers that need no database or Claude."""
from datetime import date, timedelta

from app.analysis.memo import extract_section, is_candidate, parse_verdict

TODAY = date.today().isoformat()


def row(**kwargs):
    base = {"runway_flag": "OK", "catalyst_in_window": False, "ev_cash_flag": False}
    base.update(kwargs)
    return base


def event(materiality, description="[other] x", days_ago=1):
    return {"materiality": materiality, "description": description,
            "event_date": (date.today() - timedelta(days=days_ago)).isoformat()}


def test_no_trigger_means_no_memo():
    assert not is_candidate(row(), [])


def test_each_trigger_alone_qualifies():
    assert is_candidate(row(catalyst_in_window=True), [])
    assert is_candidate(row(ev_cash_flag=True), [])
    assert is_candidate(row(), [event(4)])


def test_old_or_minor_events_do_not_qualify():
    assert not is_candidate(row(), [event(3)])
    assert not is_candidate(row(), [event(5, days_ago=20)])


def test_danger_runway_blocks_unless_financing_flagged():
    assert not is_candidate(row(runway_flag="DANGER", catalyst_in_window=True), [])
    financing = event(3, "[financing_dilutive] raised money")
    assert is_candidate(row(runway_flag="DANGER", catalyst_in_window=True), [financing])


def test_verdict_and_section_parsing():
    memo = "## Summary\nGood.\n\n## Bull case\n- a\n\n## Verdict\nText.\nVERDICT: PAPER_BUY_CANDIDATE\n\nResearch output, not financial advice."
    assert parse_verdict(memo) == "PAPER_BUY_CANDIDATE"
    assert parse_verdict("no verdict here") is None
    assert extract_section(memo, "Bull case") == "- a"
