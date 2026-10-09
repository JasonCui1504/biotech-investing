"""SEC EDGAR: filings index, XBRL financials, and 8-K text.

Run:  python -m app.ingest.edgar --tickers ALL      (or --tickers SMMT,RVMD)

Note on fiscal periods: 10-Q cash flow values are year-to-date, so we subtract
the prior period to get single-quarter numbers. Q4 is derived as FY minus the
9-month figure, so we store Q1, Q2, Q3, Q4 (the spec's "FY" row is the sum).
"""
import argparse
import logging
import re
from datetime import date, datetime
from html.parser import HTMLParser

from app.config import setup_logging
from app.db import create_tables, get_connection, run_query
from app.http_utils import get_json, get_text

log = logging.getLogger(__name__)

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
FORMS_TO_KEEP = ["10-K", "10-Q", "8-K", "4"]

# XBRL tagging varies by company, so each metric has tags to try in order.
CASH_TAGS = ["CashAndCashEquivalentsAtCarryingValue"]
SHORT_INVESTMENT_TAGS = ["MarketableSecuritiesCurrent", "ShortTermInvestments",
                         "AvailableForSaleSecuritiesDebtSecuritiesCurrent"]
LONG_INVESTMENT_TAGS = ["LongTermInvestments"]
OPERATING_CASH_FLOW_TAGS = ["NetCashProvidedByUsedInOperatingActivities"]
NET_INCOME_TAGS = ["NetIncomeLoss"]
REVENUE_TAGS = ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"]
DEBT_TAGS = ["LongTermDebt", "DebtInstrumentCarryingAmount"]


# ---------------------------------------------------------------- filings

def build_document_url(cik, accession_number, primary_document):
    """Build the browser URL of a filing's main document."""
    return (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
            f"{accession_number.replace('-', '')}/{primary_document}")


def fetch_recent_filings(ticker, cik):
    """Save recent 10-K/10-Q/8-K/Form 4 rows for a company; returns how many were new."""
    data = get_json(SUBMISSIONS_URL.format(cik=cik), pause_seconds=0.15)
    if data is None:
        log.error("%s: could not fetch submissions", ticker)
        return 0
    recent = data["filings"]["recent"]
    conn = get_connection()
    new_rows = 0
    for i in range(len(recent["accessionNumber"])):
        if recent["form"][i] not in FORMS_TO_KEEP:
            continue
        url = build_document_url(cik, recent["accessionNumber"][i], recent["primaryDocument"][i])
        cursor = conn.execute(
            "INSERT OR IGNORE INTO filings (accession_number, ticker, form_type, filing_date, "
            "primary_document_url) VALUES (?, ?, ?, ?, ?)",
            (recent["accessionNumber"][i], ticker, recent["form"][i], recent["filingDate"][i], url))
        new_rows += cursor.rowcount
    conn.commit()
    conn.close()
    return new_rows


# ------------------------------------------------------------- financials

def get_tag_facts(company_facts, tags, unit="USD", namespace="us-gaap"):
    """Return the fact list for the first tag that exists, else []."""
    available = company_facts.get("facts", {}).get(namespace, {})
    for tag in tags:
        if tag in available and unit in available[tag].get("units", {}):
            return available[tag]["units"][unit]
    return []


def months_between(start_text, end_text):
    """Approximate whole months between two ISO dates (3, 6, 9, 12...)."""
    start = date.fromisoformat(start_text)
    end = date.fromisoformat(end_text)
    return round((end - start).days / 30.4)


def cumulative_to_quarterly(facts):
    """Turn year-to-date duration facts into single-quarter values.

    facts: list of dicts with start, end, val, filed. Returns a list of dicts
    {period_end, fiscal_period, value, filed}. Within one fiscal year (same
    start date) each quarter = this cumulative value - the previous one.
    """
    # Keep the earliest-filed value for each (start, end): later filings may be
    # restated, and using the first report avoids look-ahead bias.
    first_seen = {}
    for fact in facts:
        if "start" not in fact:
            continue
        key = (fact["start"], fact["end"])
        if key not in first_seen or fact["filed"] < first_seen[key]["filed"]:
            first_seen[key] = fact

    by_year = {}
    for (start, end), fact in first_seen.items():
        months = months_between(start, end)
        if months in (3, 6, 9, 12):
            by_year.setdefault(start, []).append((months, fact))

    quarters = []
    for year_start, items in by_year.items():
        items.sort(key=lambda pair: pair[0])
        previous_value = 0
        previous_months = 0
        for months, fact in items:
            if months - previous_months != 3 and previous_months != 0:
                # A gap (missing 10-Q) means we cannot derive a single quarter.
                previous_value, previous_months = fact["val"], months
                continue
            quarters.append({"period_end": fact["end"], "fiscal_period": f"Q{months // 3}",
                             "value": fact["val"] - previous_value, "filed": fact["filed"],
                             "accn": fact.get("accn")})
            previous_value, previous_months = fact["val"], months
    return quarters


def get_instant_values(company_facts, tags):
    """Return {period_end: value} for balance-sheet facts.

    Tags are tried in order for EACH date, because companies switch tags over time
    (a tag used in 2022 may be gone by 2026). The first-reported value wins.
    """
    result = {}
    for tag in tags:
        facts = get_tag_facts(company_facts, [tag])
        values_for_tag = {}
        for fact in sorted(facts, key=lambda f: f["filed"], reverse=True):
            values_for_tag[fact["end"]] = fact["val"]  # newest first, so oldest filing overwrites last
        for end, value in values_for_tag.items():
            if end not in result:
                result[end] = value
    return result


def get_cash_and_investments(company_facts):
    """Return {period_end: cash + short-term + long-term investments}."""
    cash = get_instant_values(company_facts, CASH_TAGS)
    short = get_instant_values(company_facts, SHORT_INVESTMENT_TAGS)
    long_term = get_instant_values(company_facts, LONG_INVESTMENT_TAGS)
    total = {}
    for end, value in cash.items():
        total[end] = value + short.get(end, 0) + long_term.get(end, 0)
    return total


def get_shares_by_accession(company_facts):
    """Return {accession_number: shares outstanding on the filing cover page}."""
    facts = get_tag_facts(company_facts, ["EntityCommonStockSharesOutstanding"],
                          unit="shares", namespace="dei")
    return {fact["accn"]: fact["val"] for fact in facts if "accn" in fact}


def quarterly_by_end(company_facts, tags):
    """Return {(period_end, fiscal_period): quarter dict} for a flow metric.

    Like get_instant_values, each tag is tried for every period, in order.
    """
    result = {}
    for tag in tags:
        quarters = cumulative_to_quarterly(get_tag_facts(company_facts, [tag]))
        for q in quarters:
            key = (q["period_end"], q["fiscal_period"])
            if key not in result:
                result[key] = q
    return result


def build_financial_rows(ticker, company_facts):
    """Combine all metrics into rows for the financials table."""
    cash = get_cash_and_investments(company_facts)
    debt = get_instant_values(company_facts, DEBT_TAGS)
    shares = get_shares_by_accession(company_facts)
    cash_flow = quarterly_by_end(company_facts, OPERATING_CASH_FLOW_TAGS)
    income = quarterly_by_end(company_facts, NET_INCOME_TAGS)
    revenue = quarterly_by_end(company_facts, REVENUE_TAGS)

    rows = []
    for (end, period), quarter in cash_flow.items():
        rows.append((ticker, end, period, cash.get(end), quarter["value"],
                     income.get((end, period), {}).get("value"),
                     revenue.get((end, period), {}).get("value"),
                     shares.get(quarter["accn"]), debt.get(end), quarter["filed"]))
    return rows


def fetch_financials(ticker, cik):
    """Download XBRL company facts and save quarterly rows; returns the row count."""
    data = get_json(COMPANY_FACTS_URL.format(cik=cik), pause_seconds=0.15)
    if data is None:
        log.error("%s: could not fetch company facts", ticker)
        return 0
    rows = build_financial_rows(ticker, data)
    conn = get_connection()
    conn.executemany(
        "INSERT OR REPLACE INTO financials (ticker, period_end, fiscal_period, cash_and_investments, "
        "operating_cash_flow, net_income, revenue, shares_outstanding, total_debt, filed_date) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return len(rows)


# ------------------------------------------------------------------ 8-Ks

class TextExtractor(HTMLParser):
    """Collects visible text from HTML, skipping scripts and styles."""

    def __init__(self):
        super().__init__()
        self.pieces = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip_depth += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip_depth > 0:
            self.skip_depth -= 1

    def handle_data(self, data):
        if self.skip_depth == 0:
            self.pieces.append(data)


def html_to_text(html):
    """Strip HTML tags and collapse whitespace."""
    parser = TextExtractor()
    parser.feed(html)
    return re.sub(r"\s+", " ", " ".join(parser.pieces)).strip()


def fetch_8k_text(accession_number, max_chars=15000):
    """Download an 8-K's main document and return the first max_chars of plain text."""
    rows = run_query("SELECT primary_document_url FROM filings WHERE accession_number = ?",
                     (accession_number,))
    if not rows:
        log.error("Unknown accession number %s", accession_number)
        return None
    html = get_text(rows[0]["primary_document_url"], pause_seconds=0.15)
    if html is None:
        return None
    return html_to_text(html)[:max_chars]


# -------------------------------------------------------------------- CLI

def get_companies(tickers_arg):
    """Return (ticker, cik) pairs for ALL universe companies or a comma list."""
    if tickers_arg == "ALL":
        rows = run_query("SELECT ticker, cik FROM companies WHERE in_universe = 1 ORDER BY ticker")
    else:
        wanted = tickers_arg.split(",")
        marks = ",".join("?" * len(wanted))
        rows = run_query(f"SELECT ticker, cik FROM companies WHERE ticker IN ({marks})", wanted)
    return [(row["ticker"], row["cik"]) for row in rows]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch SEC filings and financials.")
    parser.add_argument("--tickers", default="ALL", help="ALL or comma-separated tickers")
    args = parser.parse_args()
    setup_logging()
    create_tables()
    for company_ticker, company_cik in get_companies(args.tickers):
        try:
            new_filings = fetch_recent_filings(company_ticker, company_cik)
            financial_rows = fetch_financials(company_ticker, company_cik)
            print(f"{company_ticker}: {new_filings} new filings, {financial_rows} financial rows")
        except Exception as error:  # one bad ticker must never stop the run
            log.error("%s: failed (%s)", company_ticker, error)
