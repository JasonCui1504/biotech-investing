"""Paper-trading log: record picks with a date and rationale, and close them later.

No real orders are ever placed. Entry prices are the latest stored close.
"""
from datetime import date, timedelta

from app.db import get_connection, run_query

ACTIONS = ["WATCH", "BUY_PAPER", "TRIM_PAPER", "SELL_PAPER", "AVOID"]


def get_latest_close(ticker):
    """Latest stored close price for a ticker, or None."""
    rows = run_query("SELECT close FROM prices WHERE ticker = ? ORDER BY date DESC LIMIT 1", (ticker,))
    return rows[0]["close"] if rows else None


def log_recommendation(ticker, action, rationale, bull_case="", bear_case="", key_catalyst="",
                       review_days=90):
    """Insert a recommendation (once per ticker, action and day); returns rec_id or None."""
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {ACTIONS}")
    today = date.today().isoformat()
    existing = run_query("SELECT rec_id FROM recommendations WHERE ticker = ? AND rec_date = ? "
                         "AND action = ?", (ticker, today, action))
    if existing:
        return None
    conn = get_connection()
    cursor = conn.execute(
        "INSERT INTO recommendations (ticker, rec_date, action, entry_price, rationale, bull_case, "
        "bear_case, key_catalyst, target_review_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ticker, today, action, get_latest_close(ticker), rationale, bull_case, bear_case, key_catalyst,
         (date.today() + timedelta(days=review_days)).isoformat()))
    conn.commit()
    rec_id = cursor.lastrowid
    conn.close()
    return rec_id


def close_recommendation(rec_id, exit_price=None, exit_date=None):
    """Mark a recommendation closed; defaults to the latest close and today."""
    rows = run_query("SELECT ticker FROM recommendations WHERE rec_id = ?", (rec_id,))
    if not rows:
        return False
    if exit_price is None:
        exit_price = get_latest_close(rows[0]["ticker"])
    conn = get_connection()
    conn.execute("UPDATE recommendations SET status = 'closed', exit_price = ?, exit_date = ? "
                 "WHERE rec_id = ?", (exit_price, exit_date or date.today().isoformat(), rec_id))
    conn.commit()
    conn.close()
    return True


def get_open_recommendations():
    """All open recommendations, oldest first."""
    return run_query("SELECT * FROM recommendations WHERE status = 'open' ORDER BY rec_date")


if __name__ == "__main__":
    open_recs = get_open_recommendations()
    print(f"{len(open_recs)} open paper recommendations")
    for rec in open_recs:
        print(f"  #{rec['rec_id']} {rec['rec_date']} {rec['action']} {rec['ticker']} @ {rec['entry_price']}")
