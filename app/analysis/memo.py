"""Claude (smart model) writes a short cited thesis memo for each candidate company.

Run:  python -m app.analysis.memo [--tickers ACAD,KRYS] [--max 3]
Plain Python builds the FACT SHEET; Claude may only use facts in it, and must cite them.
Memos go to reports/memos/{date}_{ticker}.md. PAPER_BUY_CANDIDATE verdicts are logged to
the recommendations table (paper trading only, no real orders, ever).
"""
import argparse
import logging
import os
import re
from datetime import date, timedelta

from app.analysis.classify_prompts import MEMO_SYSTEM_PROMPT
from app.analysis.rnpv import load_peak_sales_csv, rnpv_vs_ev
from app.analysis.screens import get_upcoming_catalysts, run_screens
from app.claude_utils import call_claude_text
from app.config import load_config, project_path, setup_logging
from app.db import create_tables, run_query
from app.tracking.recommendations import log_recommendation

log = logging.getLogger(__name__)

DISCLAIMER = "Research output, not financial advice."
VERDICTS = ["WATCH", "RESEARCH_MORE", "AVOID", "PAPER_BUY_CANDIDATE"]


# ---------------------------------------------------------------- candidates

def is_candidate(screen_row, recent_events):
    """Decide whether a company deserves a memo today (this limits Claude cost).

    Rule: runway not DANGER (or DANGER with a financing event flagged) AND at least one
    of: catalyst in window, cheap EV/cash, or an event with materiality >= 4 in 14 days.
    """
    has_financing = any(e["description"].startswith("[financing_dilutive]") for e in recent_events)
    if screen_row["runway_flag"] == "DANGER" and not has_financing:
        return False
    cutoff = (date.today() - timedelta(days=14)).isoformat()
    big_recent = any(e["materiality"] >= 4 and e["event_date"] >= cutoff for e in recent_events)
    return bool(screen_row["catalyst_in_window"] or screen_row["ev_cash_flag"] or big_recent)


def get_recent_events(ticker, days=30):
    """Events for a ticker in the last N days, newest first."""
    since = (date.today() - timedelta(days=days)).isoformat()
    return run_query("SELECT * FROM events WHERE ticker = ? AND event_date >= ? "
                     "ORDER BY event_date DESC", (ticker, since))


def select_candidates(max_count, tickers=None):
    """Screen the universe and return up to max_count candidate rows (dicts)."""
    screens = run_screens(tickers)
    chosen = []
    for row in screens.to_dict("records"):
        if tickers is not None or is_candidate(row, get_recent_events(row["ticker"])):
            chosen.append(row)
    return chosen[:max_count]


# ----------------------------------------------------------------- fact sheet

def fmt_money(value):
    """'$123M' style text, or 'n/a'."""
    return "n/a" if value is None else f"${value / 1e6:,.0f}M"


def fmt_number(value, digits=1):
    """Number with fixed digits, or 'n/a' for missing/NaN values."""
    if value is None or value != value:
        return "n/a"
    return f"{value:.{digits}f}"


def build_fact_sheet(screen_row):
    """Assemble the plain-text fact sheet (with citable references) for one company."""
    ticker = screen_row["ticker"]
    company = run_query("SELECT * FROM companies WHERE ticker = ?", (ticker,))[0]
    fin = run_query("SELECT * FROM financials WHERE ticker = ? ORDER BY period_end DESC LIMIT 1", (ticker,))
    fin_ref = (f"[source: financials {fin[0]['period_end']}, filed {fin[0]['filed_date']}]" if fin
               else "[source: financials unavailable]")
    last_price = run_query("SELECT MAX(date) AS d FROM prices WHERE ticker = ?", (ticker,))[0]["d"]
    price_ref = f"[source: prices through {last_price}]"
    burn_text = ("NOT burning cash (operating cash flow is positive)" if not screen_row["burn_per_qtr_m"]
                 else f"average quarterly burn {fmt_money(screen_row['burn_per_qtr_m'] * 1e6)}")
    income_text = (f"latest quarter revenue {fmt_money(fin[0]['revenue'])}, net income "
                   f"{fmt_money(fin[0]['net_income'])}, operating cash flow {fmt_money(fin[0]['operating_cash_flow'])}"
                   if fin else "no quarterly income data")
    lines = [f"COMPANY: {company['name'].split(' - ')[0]} ({ticker}), {company['exchange']}",
             f"Theme (LLM-tagged): {company['theme']}; modality: {company['modality']}; "
             f"lead asset: {company['lead_asset']} ({company['lead_phase']})",
             "", "FINANCIAL POSITION " + fin_ref,
             f"- Cash and investments {fmt_money(screen_row['cash_m'] * 1e6)}; {burn_text}; "
             f"runway {fmt_number(screen_row['runway_months'])} months (flag {screen_row['runway_flag']})",
             f"- {income_text}",
             f"- Share count growth: 1y {fmt_number(screen_row['dilution_1y_pct'])}%, "
             f"3y {fmt_number(screen_row['dilution_3y_pct'])}%",
             "", "VALUATION " + price_ref + " " + fin_ref,
             f"- Market cap {fmt_money(screen_row['market_cap_m'] * 1e6)}; EV/cash {fmt_number(screen_row['ev_to_cash'])}",
             f"- Price vs 52w high {fmt_number(screen_row['pct_off_high'])}%; 30d return "
             f"{fmt_number(screen_row['return_30d'])}%; 90d {fmt_number(screen_row['return_90d'])}%; "
             f"30d volatility {fmt_number(screen_row['volatility_30d'])}% annualized"]
    refs = {fin_ref, price_ref}
    lines += asset_lines(ticker, refs)
    lines += catalyst_lines(ticker, refs)
    lines += event_lines(ticker, refs)
    lines += rnpv_lines(ticker, refs)
    lines += ["", "REFERENCES (cite exactly as written)"] + sorted(refs)
    return "\n".join(lines)


def asset_lines(ticker, refs):
    """Fact-sheet lines for the extracted pipeline."""
    rows = run_query("SELECT * FROM assets WHERE ticker = ? ORDER BY asset_id LIMIT 12", (ticker,))
    lines = ["", "PIPELINE (LLM-extracted from the 10-K, unverified)"]
    for r in rows:
        ref = f"[source: {r['source_doc']}]"
        refs.add(ref)
        lines.append(f"- {r['asset_name']}: {r['mechanism'] or 'mechanism n/a'}; {r['indication'] or ''}; "
                     f"{r['phase']} {ref}")
    return lines if rows else lines + ["- none extracted"]


def catalyst_lines(ticker, refs):
    """Fact-sheet lines for upcoming catalysts and trial completion dates."""
    frame = get_upcoming_catalysts(180)
    lines = ["", "UPCOMING CATALYSTS (dates are sponsor/company estimates and often slip)"]
    for r in frame[frame["ticker"] == ticker].to_dict("records"):
        ref = f"[source: catalyst list, {r['type']}]"
        refs.add(ref)
        lines.append(f"- {r['expected_date']} ({r['date_precision']} precision): {r['type']}: {r['detail']} {ref}")
    return lines if len(lines) > 2 else lines + ["- none in the next 180 days"]


def event_lines(ticker, refs):
    """Fact-sheet lines for events in the last 30 days."""
    lines = ["", "RECENT EVENTS (last 30 days)"]
    for e in get_recent_events(ticker):
        ref = f"[source: event {e['event_date']} {str(e['source_ref'])[-40:]}]"
        refs.add(ref)
        lines.append(f"- {e['event_date']} (materiality {e['materiality']}): {e['description']} {ref}")
    return lines if len(lines) > 2 else lines + ["- none"]


def rnpv_lines(ticker, refs):
    """Fact-sheet lines for the rNPV range, always labeled with where peak sales came from."""
    result = rnpv_vs_ev(ticker)
    lines = ["", "RNPV (rough model; NOT a precise valuation)"]
    if result is None:
        return lines + ["- no peak-sales inputs available"]
    ref = "[source: rNPV model]"
    refs.add(ref)
    lines.append(f"- rNPV/EV ratio low/base/high: {fmt_number(result['ratio_low'], 2)} / "
                 f"{fmt_number(result['ratio_base'], 2)} / {fmt_number(result['ratio_high'], 2)} {ref}")
    lines.append(f"- {result['assets_valued']} pipeline assets valued from peak sales "
                 f"({result['llm_share_pct']:.0f}% use LLM suggestions, unverified); marketed products valued from "
                 f"trailing reported revenue: {fmt_money(result['commercial_value_usd'])}")
    return lines


# --------------------------------------------------------------- memo output

def parse_verdict(memo_text):
    """Return the last 'VERDICT: X' value in the memo, or None."""
    found = re.findall(r"VERDICT:\s*(" + "|".join(VERDICTS) + r")", memo_text)
    return found[-1] if found else None


def extract_section(memo_text, heading):
    """Text under '## heading' up to the next '##', or ''."""
    match = re.search(rf"##\s*{heading}[^\n]*\n(.*?)(?=\n##|\Z)", memo_text, re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else ""


def log_paper_candidate(ticker, memo_text):
    """Save a PAPER_BUY_CANDIDATE verdict as a paper recommendation (once per ticker per day)."""
    rec_id = log_recommendation(
        ticker, "BUY_PAPER", extract_section(memo_text, "Summary") + " (LLM memo, unverified)",
        extract_section(memo_text, "Bull case"), extract_section(memo_text, "Bear case"),
        extract_section(memo_text, "What would change my mind")[:300])
    return rec_id is not None


def generate_memo(screen_row):
    """Write and save one memo; returns (path, verdict) or None if Claude was unavailable."""
    ticker = screen_row["ticker"]
    path = project_path(f"reports/memos/{date.today().isoformat()}_{ticker}.md")
    if os.path.exists(path):
        return path, None  # already written today
    text = call_claude_text(f"memo:{ticker}", load_config()["claude"]["model_smart"],
                            MEMO_SYSTEM_PROMPT, build_fact_sheet(screen_row), effort="medium")
    if text is None:
        return None
    if DISCLAIMER not in text:
        text = text.rstrip() + "\n\n" + DISCLAIMER
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(f"# {ticker} thesis memo ({date.today().isoformat()})\n\n"
                f"_LLM-generated from a plain-Python fact sheet. Figures from Claude are unverified._\n\n{text}\n")
    verdict = parse_verdict(text)
    if verdict == "PAPER_BUY_CANDIDATE":
        log_paper_candidate(ticker, text)
    return path, verdict


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate thesis memos for candidates.")
    parser.add_argument("--tickers", help="comma-separated tickers (skips the candidate filter)")
    parser.add_argument("--max", type=int, default=None)
    args = parser.parse_args()
    setup_logging()
    create_tables()
    load_peak_sales_csv()
    limit = args.max or load_config()["memos"]["max_per_day"]
    chosen = select_candidates(limit, args.tickers.split(",") if args.tickers else None)
    print(f"{len(chosen)} candidates")
    for candidate in chosen:
        outcome = generate_memo(candidate)
        print(f"{candidate['ticker']}: {outcome}")
