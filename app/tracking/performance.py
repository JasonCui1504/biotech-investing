"""Measure paper recommendations against the benchmark (XBI), honestly.

Run:  python -m app.tracking.performance
"""
from datetime import date

from app.config import load_config
from app.db import run_query
from app.tracking.recommendations import get_latest_close

MIN_CLOSED_FOR_CONCLUSIONS = 30
COUNTED_ACTIONS = ["BUY_PAPER"]


def price_on_or_before(prices, day):
    """Latest close at or before day in a list of (date, close); None if none."""
    earlier = [close for d, close in prices if d <= day]
    return earlier[-1] if earlier else None


def get_prices(ticker):
    """Stored (date, close) rows, oldest first."""
    rows = run_query("SELECT date, close FROM prices WHERE ticker = ? ORDER BY date", (ticker,))
    return [(r["date"], r["close"]) for r in rows]


def compute_rec_result(entry_price, exit_price, bench_entry, bench_exit):
    """Return (return, benchmark return, excess) as fractions; None values if data is missing."""
    if not entry_price or exit_price is None:
        return None, None, None
    stock = exit_price / entry_price - 1
    if not bench_entry or bench_exit is None:
        return stock, None, None
    bench = bench_exit / bench_entry - 1
    return stock, bench, stock - bench


def summarize_results(results):
    """Win rate, average excess return, worst loss and sample size for a list of result dicts."""
    valid = [r for r in results if r["ret"] is not None]
    excess = [r["excess"] for r in valid if r["excess"] is not None]
    return {"n": len(valid),
            "win_rate": sum(r["ret"] > 0 for r in valid) / len(valid) if valid else None,
            "avg_excess": sum(excess) / len(excess) if excess else None,
            "worst_loss": min((r["ret"] for r in valid), default=None)}


def evaluate_recommendations():
    """Return one result dict per BUY_PAPER recommendation (open or closed)."""
    benchmark = get_prices(load_config()["benchmark_ticker"])
    results = []
    recs = run_query("SELECT * FROM recommendations WHERE action IN ('BUY_PAPER') ORDER BY rec_date")
    for rec in recs:
        prices = get_prices(rec["ticker"])
        end_day = rec["exit_date"] or date.today().isoformat()
        exit_price = rec["exit_price"] if rec["status"] == "closed" else get_latest_close(rec["ticker"])
        stock, bench, excess = compute_rec_result(
            rec["entry_price"], exit_price, price_on_or_before(benchmark, rec["rec_date"]),
            price_on_or_before(benchmark, end_day))
        results.append({"rec_id": rec["rec_id"], "ticker": rec["ticker"], "rec_date": rec["rec_date"],
                        "status": rec["status"], "ret": stock, "bench": bench, "excess": excess})
    return results


def position_size(capital, open_count, max_position_pct):
    """Equal-weight dollar size per position, capped at max_position_pct of capital."""
    if open_count <= 0:
        return 0.0
    return min(capital / open_count, capital * max_position_pct / 100)


def paper_portfolio_value():
    """Current value of the equal-weight paper portfolio (cash + positions)."""
    config = load_config()
    capital = config["tracking"]["starting_capital_usd"]
    open_recs = [r for r in run_query("SELECT * FROM recommendations WHERE action = 'BUY_PAPER' "
                                      "AND status = 'open'") if r["entry_price"]]
    size = position_size(capital, len(open_recs), config["screens"]["max_position_pct_of_portfolio"])
    invested = value = 0.0
    for rec in open_recs:
        last = get_latest_close(rec["ticker"]) or rec["entry_price"]
        invested += size
        value += size * last / rec["entry_price"]
    return {"capital": capital, "positions": len(open_recs), "position_size": size,
            "value": capital - invested + value}


def build_performance_text():
    """Plain-text performance summary including the small-sample warning."""
    results = evaluate_recommendations()
    closed = [r for r in results if r["status"] == "closed"]
    all_stats = summarize_results(results)
    lines = [f"Paper recommendations: {len(results)} total, {len(closed)} closed."]
    if all_stats["n"]:
        lines.append(f"Win rate {all_stats['win_rate'] * 100:.0f}%, worst loss {all_stats['worst_loss'] * 100:.1f}%"
                     + (f", average excess vs XBI {all_stats['avg_excess'] * 100:+.1f}%"
                        if all_stats["avg_excess"] is not None else "")
                     + f" (n={all_stats['n']}).")
    portfolio = paper_portfolio_value()
    lines.append(f"Paper portfolio: ${portfolio['value']:,.0f} from ${portfolio['capital']:,.0f} starting capital, "
                 f"{portfolio['positions']} open positions at ${portfolio['position_size']:,.0f} each.")
    if len(closed) < MIN_CLOSED_FOR_CONCLUSIONS:
        lines.append(f"WARNING: only {len(closed)} closed recommendations; fewer than "
                     f"{MIN_CLOSED_FOR_CONCLUSIONS} is too few to conclude anything.")
    return "\n".join(lines)


if __name__ == "__main__":
    print(build_performance_text())
