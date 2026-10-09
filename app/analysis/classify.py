"""Claude (fast model) classifies news headlines and 8-K filings into events.

Run:  python -m app.analysis.classify
"""
import logging
from datetime import date, datetime

from app.analysis.classify_prompts import CLASSIFY_SYSTEM_PROMPT
from app.analysis.extract import extract_press_release
from app.claude_utils import call_claude_json
from app.config import load_config, setup_logging
from app.db import create_tables, get_connection, run_query
from app.ingest.edgar import fetch_8k_text

log = logging.getLogger(__name__)

BATCH_SIZE = 20
EVENT_TYPES = ["trial_data_positive", "trial_data_negative", "trial_data_mixed", "fda_approval",
               "fda_rejection", "fda_other", "financing_dilutive", "partnership_or_ma",
               "management_change", "earnings", "guidance_change", "other"]


def validate_item(item, known_tickers):
    """Clean one classifier result: bad types become 'other', unknown tickers become None."""
    ticker = item.get("ticker")
    if ticker not in known_tickers:
        ticker = None
    event_type = item.get("event_type")
    if event_type not in EVENT_TYPES:
        event_type = "other"
    try:
        materiality = max(1, min(5, int(item.get("materiality"))))
    except (TypeError, ValueError):
        materiality = 1
    return {"ticker": ticker, "event_type": event_type, "materiality": materiality,
            "summary": item.get("one_line_summary") or ""}


def get_known_companies():
    """Return {ticker: name} for in-universe companies."""
    rows = run_query("SELECT ticker, name FROM companies WHERE in_universe = 1")
    return {row["ticker"]: row["name"].split(" - ")[0] for row in rows}


def save_event(ticker, event_date, event_type, summary, materiality, source_ref):
    """Insert a news event (duplicates are ignored)."""
    conn = get_connection()
    conn.execute(
        "INSERT OR IGNORE INTO events (ticker, event_date, event_type, description, materiality, "
        "source_ref, created_at) VALUES (?, ?, 'news', ?, ?, ?, ?)",
        (ticker, event_date, f"[{event_type}] {summary}", materiality, source_ref,
         datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()


def classify_batch(rows, known):
    """Classify up to BATCH_SIZE news rows with one Claude call; returns number classified."""
    company_lines = "\n".join(f"{t}: {n}" for t, n in known.items())
    item_lines = "\n".join(f"id={r['item_id']} | {r['title']} | {(r['snippet'] or '')[:300]}"
                           for r in rows)
    user_text = f"KNOWN COMPANIES:\n{company_lines}\n\nITEMS:\n{item_lines}"
    model = load_config()["claude"]["model_fast"]
    result = call_claude_json("classify_news", model, CLASSIFY_SYSTEM_PROMPT, user_text)
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        return 0
    by_id = {r["item_id"]: r for r in rows}
    done = 0
    conn = get_connection()
    for item in result["items"]:
        row = by_id.get(item.get("id"))
        if row is None:
            continue
        clean = validate_item(item, known)
        conn.execute("UPDATE news_items SET ticker=?, event_type=?, materiality=?, classified=1 "
                     "WHERE item_id=?", (clean["ticker"], clean["event_type"], clean["materiality"],
                                         row["item_id"]))
        done += 1
        if clean["ticker"] and clean["materiality"] >= 3:
            conn.commit()
            event_date = (row["published_at"] or date.today().isoformat())[:10]
            save_event(clean["ticker"], event_date, clean["event_type"], clean["summary"],
                       clean["materiality"], row["url"])
    conn.commit()
    conn.close()
    return done


def classify_news_items():
    """Classify every unclassified, non-8-K news item in batches; returns the total classified."""
    known = get_known_companies()
    rows = run_query("SELECT * FROM news_items WHERE classified = 0 AND source != '8-K' "
                     "ORDER BY item_id")
    total = 0
    for start in range(0, len(rows), BATCH_SIZE):
        total += classify_batch(rows[start:start + BATCH_SIZE], known)
    return total


def classify_8k_filing(filing, known):
    """Classify one 8-K from its own text; also saves a news item and extracts catalysts."""
    text = fetch_8k_text(filing["accession_number"])
    if not text:
        return False
    user_text = f"KNOWN COMPANIES:\n{filing['ticker']}: {known.get(filing['ticker'], '')}\n\n" \
                f"ITEMS:\nid=1 | 8-K filed {filing['filing_date']} | {text}"
    model = load_config()["claude"]["model_fast"]
    result = call_claude_json("classify_8k", model, CLASSIFY_SYSTEM_PROMPT, user_text)
    if not isinstance(result, dict) or not result.get("items"):
        return False
    clean = validate_item(result["items"][0], known)
    clean["ticker"] = filing["ticker"]  # an 8-K is always tied to one company
    conn = get_connection()
    conn.execute(
        "INSERT OR IGNORE INTO news_items (url, title, source, published_at, fetched_at, snippet, "
        "ticker, event_type, materiality, classified) VALUES (?, ?, '8-K', ?, ?, ?, ?, ?, ?, 1)",
        (filing["primary_document_url"], f"{filing['ticker']} 8-K: {clean['summary']}"[:200],
         filing["filing_date"], datetime.now().isoformat(timespec="seconds"), text[:500],
         clean["ticker"], clean["event_type"], clean["materiality"]))
    conn.execute("UPDATE filings SET processed = 1 WHERE accession_number = ?",
                 (filing["accession_number"],))
    conn.commit()
    conn.close()
    if clean["materiality"] >= 3:
        save_event(filing["ticker"], filing["filing_date"], clean["event_type"], clean["summary"],
                   clean["materiality"], filing["primary_document_url"])
        extract_press_release(filing["ticker"], text, filing["primary_document_url"])
    return True


def classify_new_8ks(days_back=7):
    """Classify unprocessed 8-Ks filed in the last days_back days for universe companies."""
    known = get_known_companies()
    cutoff = date.fromordinal(date.today().toordinal() - days_back).isoformat()
    filings = run_query("SELECT * FROM filings WHERE form_type = '8-K' AND processed = 0 "
                        "AND filing_date >= ? ORDER BY filing_date", (cutoff,))
    done = 0
    for filing in filings:
        if filing["ticker"] in known:
            try:
                done += 1 if classify_8k_filing(filing, known) else 0
            except Exception as error:  # one bad filing must not stop the rest
                log.error("%s: 8-K %s failed (%s)", filing["ticker"], filing["accession_number"], error)
    return done


if __name__ == "__main__":
    setup_logging()
    create_tables()
    print(f"News items classified: {classify_news_items()}")
    print(f"8-Ks classified: {classify_new_8ks()}")
