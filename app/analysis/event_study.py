"""Event study: how did stocks react to past data, FDA and financing events?

Run:  python -m app.analysis.event_study
NOTE: this needs several months of event history before the numbers mean anything.
The sample size is printed next to every statistic; small samples are noise.
Timing caveat: news after the close shows up in the NEXT day's return, and the 1-day
return here uses the first trading day on or after the event date.
"""
import logging
import statistics

from app.config import load_config, setup_logging
from app.db import create_tables, get_connection, run_query

log = logging.getLogger(__name__)

STUDIED_TYPES = ["trial_data_positive", "trial_data_negative", "trial_data_mixed",
                 "fda_approval", "fda_rejection", "financing_dilutive"]


def get_event_class(description):
    """Read the classifier label from a description like '[fda_approval] text'; else None."""
    if description and description.startswith("[") and "]" in description:
        return description[1:description.index("]")]
    return None


def window_return(prices, event_date, trading_days):
    """Return from the last close before event_date to the close N trading days in.

    prices: list of (iso_date, close) sorted by date. trading_days=1 means the first
    trading day on or after event_date. Returns None if the data does not cover it.
    """
    before = [close for day, close in prices if day < event_date]
    after = [close for day, close in prices if day >= event_date]
    if not before or len(after) < trading_days or not before[-1]:
        return None
    return after[trading_days - 1] / before[-1] - 1


def compute_event_reaction(stock_prices, benchmark_prices, event_date):
    """Return ret_1d, ret_5d and benchmark-adjusted (excess) versions, or None values."""
    result = {}
    for days in (1, 5):
        stock = window_return(stock_prices, event_date, days)
        bench = window_return(benchmark_prices, event_date, days)
        result[f"ret_{days}d"] = stock
        result[f"excess_{days}d"] = None if stock is None or bench is None else stock - bench
    return result


def get_prices(ticker):
    """All stored (date, close) rows for a ticker, oldest first."""
    rows = run_query("SELECT date, close FROM prices WHERE ticker = ? ORDER BY date", (ticker,))
    return [(r["date"], r["close"]) for r in rows]


def get_event_reactions():
    """Compute reactions for every studied event and save them; returns the number saved."""
    benchmark = get_prices(load_config()["benchmark_ticker"])
    events = run_query("SELECT * FROM events WHERE event_type = 'news' AND ticker IS NOT NULL")
    conn = get_connection()
    saved = 0
    for event in events:
        event_class = get_event_class(event["description"])
        if event_class not in STUDIED_TYPES:
            continue
        reaction = compute_event_reaction(get_prices(event["ticker"]), benchmark, event["event_date"])
        conn.execute(
            "INSERT OR REPLACE INTO event_reactions (event_id, ticker, event_type, event_date, ret_1d, "
            "ret_5d, excess_1d, excess_5d) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (event["event_id"], event["ticker"], event_class, event["event_date"], reaction["ret_1d"],
             reaction["ret_5d"], reaction["excess_1d"], reaction["excess_5d"]))
        saved += 1
    conn.commit()
    conn.close()
    return saved


def summarize_reactions():
    """Mean and median reaction by event type, with sample sizes; returns a list of dicts."""
    summary = []
    for event_type in STUDIED_TYPES:
        rows = run_query("SELECT * FROM event_reactions WHERE event_type = ?", (event_type,))
        line = {"event_type": event_type}
        for column in ("ret_1d", "ret_5d", "excess_5d"):
            values = [r[column] for r in rows if r[column] is not None]
            line[column] = {"n": len(values),
                            "mean": statistics.mean(values) if values else None,
                            "median": statistics.median(values) if values else None}
        summary.append(line)
    return summary


def format_percent(value):
    """'12.3%' or 'n/a'."""
    return "n/a" if value is None else f"{value * 100:.1f}%"


if __name__ == "__main__":
    setup_logging()
    create_tables()
    print(f"Event reactions computed: {get_event_reactions()}")
    for line in summarize_reactions():
        five = line["ret_5d"]
        print(f"{line['event_type']:<22} 5-day return: mean {format_percent(five['mean'])}, "
              f"median {format_percent(five['median'])} (n={five['n']}); "
              f"excess vs XBI mean {format_percent(line['excess_5d']['mean'])} (n={line['excess_5d']['n']})")
