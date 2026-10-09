"""Downloads end-of-day prices with yfinance and stores them in the prices table.

Run:  python -m app.ingest.prices            (universe + XBI + IBB)
      python -m app.ingest.prices --tickers SMMT,RVMD
Fallbacks if yfinance breaks (not implemented): Stooq CSV downloads, Tiingo free tier.
"""
import argparse
import logging
from datetime import date, timedelta

from app.config import load_config, setup_logging
from app.db import create_tables, get_connection, run_query

log = logging.getLogger(__name__)


def get_last_stored_date(ticker):
    """Return the latest date stored for a ticker (ISO string), or None."""
    rows = run_query("SELECT MAX(date) AS last_date FROM prices WHERE ticker = ?", (ticker,))
    return rows[0]["last_date"]


def download_one_ticker(ticker, start_date):
    """Download daily bars for one ticker starting at start_date; returns a DataFrame or None."""
    import yfinance as yf
    try:
        frame = yf.download(ticker, start=start_date, auto_adjust=False,
                            progress=False, multi_level_index=False)
    except Exception as error:
        log.error("%s: price download failed (%s)", ticker, error)
        return None
    if frame is None or frame.empty:
        log.warning("%s: no price data returned", ticker)
        return None
    return frame


def frame_to_rows(ticker, frame):
    """Convert a yfinance DataFrame into a list of tuples matching the prices table."""
    rows = []
    for day, bar in frame.iterrows():
        rows.append((ticker, day.strftime("%Y-%m-%d"), float(bar["Open"]), float(bar["High"]),
                     float(bar["Low"]), float(bar["Close"]), float(bar["Adj Close"]),
                     float(bar["Volume"])))
    return rows


def update_prices(tickers, days_back=365):
    """Fetch new daily prices for each ticker; returns {ticker: rows_saved}."""
    create_tables()
    conn = get_connection()
    results = {}
    for ticker in tickers:
        last_date = get_last_stored_date(ticker)
        if last_date:
            # Start the day after the last stored date so reruns add nothing twice.
            start = (date.fromisoformat(last_date) + timedelta(days=1)).isoformat()
        else:
            start = (date.today() - timedelta(days=days_back)).isoformat()
        if start > date.today().isoformat():
            results[ticker] = 0
            continue
        frame = download_one_ticker(ticker, start)
        if frame is None:
            results[ticker] = 0
            continue
        rows = frame_to_rows(ticker, frame)
        conn.executemany(
            "INSERT OR REPLACE INTO prices (ticker, date, open, high, low, close, adj_close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        conn.commit()
        results[ticker] = len(rows)
    conn.close()
    return results


def get_universe_tickers():
    """Tickers of all in-universe companies."""
    rows = run_query("SELECT ticker FROM companies WHERE in_universe = 1 ORDER BY ticker")
    return [row["ticker"] for row in rows]


def get_price_tickers():
    """Universe tickers plus the benchmark and extra ETFs from config."""
    config = load_config()
    extras = [config["benchmark_ticker"]] + config.get("extra_price_tickers", [])
    return get_universe_tickers() + extras


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Update daily prices.")
    parser.add_argument("--tickers", help="comma-separated tickers (default: universe + XBI + IBB)")
    parser.add_argument("--days-back", type=int, default=365)
    args = parser.parse_args()
    setup_logging()
    if args.tickers:
        ticker_list = args.tickers.split(",")
    else:
        ticker_list = get_price_tickers()
    outcome = update_prices(ticker_list, args.days_back)
    missing = [t for t, n in outcome.items() if n == 0]
    print(f"Saved {sum(outcome.values())} price rows for {len(outcome) - len(missing)} tickers.")
    if missing:
        print(f"No new data for: {', '.join(missing)}")
