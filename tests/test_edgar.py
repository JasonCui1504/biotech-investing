"""Offline tests for the EDGAR helpers (hand-made sample data, no network)."""
from app.ingest.edgar import (build_document_url, cumulative_to_quarterly,
                              get_cash_and_investments, html_to_text)


def make_fact(start, end, val, filed):
    return {"start": start, "end": end, "val": val, "filed": filed, "accn": "acc-" + end}


def test_cumulative_cash_flow_becomes_single_quarters():
    # Burn is reported year-to-date: -10 (3 mo), -25 (6 mo), -45 (9 mo), -70 (12 mo).
    facts = [
        make_fact("2025-01-01", "2025-03-31", -10, "2025-05-01"),
        make_fact("2025-01-01", "2025-06-30", -25, "2025-08-01"),
        make_fact("2025-01-01", "2025-09-30", -45, "2025-11-01"),
        make_fact("2025-01-01", "2025-12-31", -70, "2026-02-20"),
    ]
    quarters = cumulative_to_quarterly(facts)
    values = {q["fiscal_period"]: q["value"] for q in quarters}
    assert values == {"Q1": -10, "Q2": -15, "Q3": -20, "Q4": -25}


def test_missing_quarter_is_skipped_not_guessed():
    facts = [
        make_fact("2025-01-01", "2025-03-31", -10, "2025-05-01"),
        make_fact("2025-01-01", "2025-09-30", -45, "2025-11-01"),  # no 6-month fact
    ]
    quarters = cumulative_to_quarterly(facts)
    assert [q["fiscal_period"] for q in quarters] == ["Q1"]


def test_restated_value_uses_first_filing():
    facts = [
        make_fact("2025-01-01", "2025-03-31", -10, "2025-05-01"),
        make_fact("2025-01-01", "2025-03-31", -12, "2026-05-01"),  # comparative in later filing
    ]
    assert cumulative_to_quarterly(facts)[0]["value"] == -10


def test_cash_and_investments_adds_up_on_same_date():
    company_facts = {"facts": {"us-gaap": {
        "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [
            {"end": "2025-12-31", "val": 100, "filed": "2026-02-20"}]}},
        "MarketableSecuritiesCurrent": {"units": {"USD": [
            {"end": "2025-12-31", "val": 50, "filed": "2026-02-20"}]}},
    }}}
    assert get_cash_and_investments(company_facts) == {"2025-12-31": 150}


def test_document_url_and_html_text():
    url = build_document_url("0001599298", "0001599298-26-000012", "form8k.htm")
    assert url == "https://www.sec.gov/Archives/edgar/data/1599298/000159929826000012/form8k.htm"
    assert html_to_text("<p>Hello <b>world</b></p><script>x=1</script>") == "Hello world"


def test_stale_tag_does_not_hide_newer_tag():
    # Company used ShortTermInvestments in 2023 but switched tags by 2026.
    company_facts = {"facts": {"us-gaap": {
        "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [
            {"end": "2026-06-30", "val": 20, "filed": "2026-08-01"}]}},
        "ShortTermInvestments": {"units": {"USD": [
            {"end": "2023-12-31", "val": 99, "filed": "2024-02-01"}]}},
        "AvailableForSaleSecuritiesDebtSecuritiesCurrent": {"units": {"USD": [
            {"end": "2026-06-30", "val": 70, "filed": "2026-08-01"}]}},
    }}}
    assert get_cash_and_investments(company_facts)["2026-06-30"] == 90
