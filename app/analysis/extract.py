"""Claude turns 10-K and press-release text into structured pipeline data.

Run:  python -m app.analysis.extract --tickers SMMT
Everything saved here is LLM-extracted and unverified; the source columns say so.
"""
import argparse
import logging
import re
from datetime import date, datetime

from app.analysis.classify_prompts import PIPELINE_SYSTEM_PROMPT, PRESS_RELEASE_SYSTEM_PROMPT
from app.claude_utils import call_claude_json
from app.config import load_config, setup_logging
from app.db import create_tables, get_connection, run_query
from app.http_utils import get_text
from app.ingest.edgar import html_to_text

log = logging.getLogger(__name__)

MAX_SECTION_CHARS = 60000
MONTHS = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
          "august": 8, "september": 9, "october": 10, "november": 11, "december": 12}


# ----------------------------------------------------------- timing phrases

def parse_timing_phrase(text):
    """Turn a phrase like 'Q1 2027' into (iso_date, precision); (None, None) if unknown.

    Periods map to their first day (Q1 -> Jan 1), except 'year-end' phrases which
    map to Dec 31. Precision is exact, month, quarter, half or year.
    """
    if not text:
        return None, None
    lower = text.lower().strip()

    exact = re.search(r"(\d{4})-(\d{2})-(\d{2})", lower)
    if exact:
        return exact.group(0), "exact"
    month_day = re.search(r"([a-z]+)\s+(\d{1,2}),?\s+(\d{4})", lower)
    if month_day and month_day.group(1) in MONTHS:
        month = MONTHS[month_day.group(1)]
        return f"{month_day.group(3)}-{month:02d}-{int(month_day.group(2)):02d}", "exact"

    quarter = re.search(r"\bq([1-4])\s*(?:of\s*)?(\d{4})", lower)
    if quarter:
        month = (int(quarter.group(1)) - 1) * 3 + 1
        return f"{quarter.group(2)}-{month:02d}-01", "quarter"

    half = re.search(r"\b([12])h\s*(?:of\s*)?(\d{4})|\bh([12])\s*(\d{4})", lower)
    if half:
        number = half.group(1) or half.group(3)
        year = half.group(2) or half.group(4)
        return f"{year}-{'01' if number == '1' else '07'}-01", "half"
    half_words = re.search(r"(first|second) half of (\d{4})", lower)
    if half_words:
        return f"{half_words.group(2)}-{'01' if half_words.group(1) == 'first' else '07'}-01", "half"

    mid = re.search(r"mid[- ]?(\d{4})", lower)
    if mid:
        return f"{mid.group(1)}-07-01", "half"  # "mid-year" is treated as July 1

    year_end = re.search(r"(?:year[- ]?end|end of)\s*(?:of\s*)?(\d{4})", lower)
    if year_end:
        return f"{year_end.group(1)}-12-31", "year"

    month_year = re.search(r"([a-z]+)\s+(\d{4})", lower)
    if month_year and month_year.group(1) in MONTHS:
        return f"{month_year.group(2)}-{MONTHS[month_year.group(1)]:02d}-01", "month"

    year_only = re.search(r"\b(20\d{2})\b", lower)
    if year_only:
        return f"{year_only.group(1)}-01-01", "year"
    return None, None


def guess_catalyst_type(milestone_text):
    """Pick a catalyst type from keywords in the milestone description."""
    lower = (milestone_text or "").lower()
    if re.search(r"pdufa|approval|\bnda\b|\bbla\b", lower):
        return "pdufa"
    if "advisory" in lower or "adcom" in lower:
        return "adcom"
    if re.search(r"initiat|first patient|\bstart", lower):
        return "phase_start"
    return "topline_data"


# ------------------------------------------------------------ 10-K sections

def find_business_section(text):
    """Return the Item 1 (Business) section of a 10-K as plain text.

    The table of contents also contains 'Item 1. Business', so we try every match
    and keep the one with the longest stretch before 'Item 1A'.
    """
    starts = [m.start() for m in re.finditer(r"item\s*1\.?\s*business", text, re.IGNORECASE)]
    best = ""
    for start in starts:
        end_match = re.search(r"item\s*1a\.?\s*risk\s*factors", text[start:], re.IGNORECASE)
        end = start + end_match.start() if end_match else min(len(text), start + MAX_SECTION_CHARS)
        if end - start > len(best):
            best = text[start:end]
    if len(best) < 5000:  # section detection failed; fall back to the beginning
        return text[:MAX_SECTION_CHARS]
    return best


def get_latest_10k(ticker):
    """Return (accession_number, url) of the newest 10-K in the filings table, or None."""
    rows = run_query("SELECT accession_number, primary_document_url FROM filings "
                     "WHERE ticker = ? AND form_type = '10-K' ORDER BY filing_date DESC LIMIT 1",
                     (ticker,))
    if not rows:
        return None
    return rows[0]["accession_number"], rows[0]["primary_document_url"]


def get_latest_10k_text(ticker):
    """Download the newest 10-K and return (accession, business_text); None if unavailable."""
    latest = get_latest_10k(ticker)
    if latest is None:
        return None
    accession, url = latest
    html = get_text(url, pause_seconds=0.15)
    if html is None:
        return None
    business = find_business_section(html_to_text(html))
    if len(business) > MAX_SECTION_CHARS:
        business = business[:MAX_SECTION_CHARS] + "\n[TRUNCATED: section longer than 60,000 characters]"
    return accession, business


# ------------------------------------------------------------------ saving

def save_assets(ticker, assets, source_doc):
    """Save extracted assets; keeps any peak-sales number the user already entered."""
    conn = get_connection()
    now = datetime.now().isoformat(timespec="seconds")
    for asset in assets:
        if not asset.get("asset_name"):
            continue
        conn.execute(
            "INSERT INTO assets (ticker, asset_name, mechanism, indication, phase, source_doc, "
            "extracted_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(ticker, asset_name, indication) DO UPDATE SET mechanism=excluded.mechanism, "
            "phase=excluded.phase, source_doc=excluded.source_doc, extracted_at=excluded.extracted_at",
            (ticker, asset["asset_name"], asset.get("mechanism"), asset.get("indication") or "",
             asset.get("phase"), source_doc, now))
    conn.commit()
    conn.close()


def save_catalyst(conn, ticker, asset_name, milestone, timing, source, source_url):
    """Save one catalyst if its timing phrase can be parsed; returns True if saved."""
    expected_date, precision = parse_timing_phrase(timing)
    if expected_date is None:
        return False
    conn.execute(
        "INSERT OR IGNORE INTO catalysts (ticker, asset_name, catalyst_type, expected_date, "
        "date_precision, source, source_url, last_confirmed) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ticker, asset_name or "", guess_catalyst_type(milestone), expected_date, precision, source,
         source_url, date.today().isoformat()))
    return True


def save_pipeline_catalysts(ticker, assets, source_url):
    """Turn each asset's next_milestone/expected_timing into a catalysts row."""
    conn = get_connection()
    saved = 0
    for asset in assets:
        if save_catalyst(conn, ticker, asset.get("asset_name"), asset.get("next_milestone"),
                         asset.get("expected_timing"), "10-K (LLM-extracted, unverified)", source_url):
            saved += 1
    conn.commit()
    conn.close()
    return saved


# --------------------------------------------------------------- Claude calls

def extract_pipeline(ticker):
    """Extract assets and catalysts from a ticker's newest 10-K; returns the JSON dict or None.

    Skips filings already processed, so a 10-K is only sent to Claude once.
    """
    latest = get_latest_10k(ticker)
    if latest is None:
        log.warning("%s: no 10-K in filings table (run the edgar step first)", ticker)
        return None
    accession, url = latest
    done = run_query("SELECT processed FROM filings WHERE accession_number = ?", (accession,))
    if done and done[0]["processed"] == 1:
        return None
    document = get_latest_10k_text(ticker)
    if document is None:
        return None
    model = load_config()["claude"]["model_smart"]
    result = call_claude_json(f"extract_pipeline:{ticker}", model, PIPELINE_SYSTEM_PROMPT,
                              document[1], effort="medium")
    if not isinstance(result, dict):
        return None
    assets = result.get("assets") or []
    save_assets(ticker, assets, f"10-K {accession} (LLM-extracted, unverified)")
    save_pipeline_catalysts(ticker, assets, url)
    conn = get_connection()
    conn.execute("UPDATE filings SET processed = 1 WHERE accession_number = ?", (accession,))
    conn.commit()
    conn.close()
    return result


def extract_press_release(ticker, text, source_url):
    """Pull catalysts and data results out of an 8-K press release; returns the JSON dict."""
    model = load_config()["claude"]["model_fast"]
    result = call_claude_json(f"press_release:{ticker}", model, PRESS_RELEASE_SYSTEM_PROMPT, text)
    if not isinstance(result, dict):
        return None
    conn = get_connection()
    for item in result.get("catalysts") or []:
        save_catalyst(conn, ticker, item.get("asset_name"), item.get("catalyst_type"),
                      item.get("expected_timing"), "8-K (LLM-extracted, unverified)", source_url)
    conn.commit()
    conn.close()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract pipelines from 10-Ks with Claude.")
    parser.add_argument("--tickers", required=True, help="comma-separated tickers")
    args = parser.parse_args()
    setup_logging()
    create_tables()
    for item in args.tickers.split(","):
        outcome = extract_pipeline(item)
        count = len(outcome.get("assets") or []) if outcome else 0
        print(f"{item}: {count} assets extracted" if outcome else f"{item}: nothing new")
