"""Plain-Python metrics (no Claude): burn, runway, EV/cash, dilution, price context, catalysts.

Run:  python -m app.analysis.screens [--tickers SMMT,RVMD]
Pure helper functions (compute_*) take plain numbers so they are easy to test.
"""
import argparse
import math
from datetime import date, timedelta

import pandas as pd

from app.config import load_config
from app.db import get_connection, run_query


# ------------------------------------------------------- pure calculations

def compute_burn(operating_cash_flows):
    """Average quarterly burn from recent quarterly operating cash flows (newest first).

    Uses up to 4 quarters (at least 2). Returns a positive number, or None if the
    company is not burning cash (average operating cash flow is zero or positive).
    """
    recent = [x for x in operating_cash_flows[:4] if x is not None]
    if len(recent) < 2:
        return None
    average = sum(recent) / len(recent)
    if average >= 0:
        return None
    return -average


def compute_runway_months(cash, quarterly_burn):
    """Months of cash left at the current burn rate (None if not burning or no cash data)."""
    # Runway = how long until the company must raise money (dilution risk).
    if cash is None or quarterly_burn is None:
        return None
    return cash / (quarterly_burn / 3)


def compute_enterprise_value(market_cap, debt, cash):
    """EV = market cap + debt - cash. Missing debt counts as zero."""
    if market_cap is None or cash is None:
        return None
    return market_cap + (debt or 0) - cash


def compute_pct_change(old_value, new_value):
    """Percent change from old to new (None if old is missing or zero)."""
    if not old_value or new_value is None:
        return None
    return (new_value - old_value) / old_value * 100


def runway_flag(runway_months, config):
    """OK / WATCH / DANGER from runway months; None runway means not burning (OK)."""
    ok_months = config["screens"]["min_runway_months_ok"]
    warning_months = config["screens"]["runway_warning_months"]
    if runway_months is None:
        return "OK"
    if runway_months > ok_months:
        return "OK"
    if runway_months >= warning_months:
        return "WATCH"
    return "DANGER"


# ----------------------------------------------------------- database reads

def get_financial_rows(ticker):
    """All financial rows for a ticker, newest period first."""
    return run_query("SELECT * FROM financials WHERE ticker = ? ORDER BY period_end DESC", (ticker,))


def get_latest_close(ticker):
    """Most recent (unadjusted) close price, or None."""
    rows = run_query("SELECT close FROM prices WHERE ticker = ? ORDER BY date DESC LIMIT 1", (ticker,))
    return rows[0]["close"] if rows else None


def get_quarterly_burn(ticker):
    """Average quarterly cash burn for a ticker (positive number) or None."""
    rows = get_financial_rows(ticker)
    return compute_burn([row["operating_cash_flow"] for row in rows])


def get_latest_value(ticker, column):
    """Newest non-null value of a financials column, or None."""
    for row in get_financial_rows(ticker):
        if row[column] is not None:
            return row[column]
    return None


def get_runway_months(ticker):
    """Months of runway for a ticker, or None if not burning."""
    return compute_runway_months(get_latest_value(ticker, "cash_and_investments"),
                                 get_quarterly_burn(ticker))


def get_market_cap(ticker):
    """Latest close times latest shares outstanding, or None."""
    price = get_latest_close(ticker)
    shares = get_latest_value(ticker, "shares_outstanding")
    if price is None or shares is None:
        return None
    return price * shares


def get_enterprise_value(ticker):
    """Enterprise value in dollars, or None if data is missing."""
    return compute_enterprise_value(get_market_cap(ticker), get_latest_value(ticker, "total_debt"),
                                    get_latest_value(ticker, "cash_and_investments"))


def get_ev_to_cash(ticker):
    """EV divided by cash. Below ~0 means the market values the pipeline at less than nothing
    (rare; verify the data before trusting it)."""
    cash = get_latest_value(ticker, "cash_and_investments")
    ev = get_enterprise_value(ticker)
    if ev is None or not cash:
        return None
    return ev / cash


def get_dilution_history(ticker):
    """Percent growth in shares outstanding over about 1 and 3 years: (pct_1y, pct_3y)."""
    rows = [r for r in get_financial_rows(ticker) if r["shares_outstanding"]]
    if not rows:
        return None, None
    newest = rows[0]
    newest_date = date.fromisoformat(newest["period_end"])
    results = []
    for years in (1, 3):
        target = newest_date - timedelta(days=365 * years)
        older = [r for r in rows if date.fromisoformat(r["period_end"]) <= target + timedelta(days=45)]
        results.append(compute_pct_change(older[0]["shares_outstanding"], newest["shares_outstanding"])
                       if older else None)
    return results[0], results[1]


def get_price_context(ticker):
    """52-week high/low, % off high, 30d and 90d return, 30d annualized volatility (dict)."""
    rows = run_query("SELECT date, close FROM prices WHERE ticker = ? ORDER BY date", (ticker,))
    if len(rows) < 31:
        return {}
    prices = pd.Series([r["close"] for r in rows], index=pd.to_datetime([r["date"] for r in rows]))
    last = prices.iloc[-1]
    year = prices[prices.index >= prices.index[-1] - pd.Timedelta(days=365)]
    context = {"high_52w": year.max(), "low_52w": year.min(),
               "pct_off_high": (last / year.max() - 1) * 100}
    for days in (30, 90):
        past = prices[prices.index <= prices.index[-1] - pd.Timedelta(days=days)]
        context[f"return_{days}d"] = (last / past.iloc[-1] - 1) * 100 if len(past) else None
    daily_returns = prices.pct_change().dropna().tail(30)
    # Annualize daily volatility with sqrt(252 trading days).
    context["volatility_30d"] = daily_returns.std() * math.sqrt(252) * 100
    return context


def period_end_date(expected_date, precision):
    """Last day of the period a catalyst date refers to (e.g. 'Q4 2026' -> 2026-12-31).

    Stored dates are the FIRST day of the period, so comparing them to today would
    wrongly treat a catalyst expected 'in Q4 2026' as past once October 2 arrives.
    """
    start = date.fromisoformat(expected_date)
    months = {"month": 1, "quarter": 3, "half": 6, "year": 12}.get(precision)
    if months is None or (precision == "year" and expected_date.endswith("12-31")):
        return start
    total = start.month - 1 + months
    next_start = date(start.year + total // 12, total % 12 + 1, 1)
    return next_start - timedelta(days=1)


def get_upcoming_catalysts(days_ahead=120):
    """Catalysts plus trial primary-completion dates in the next days_ahead, sorted by date."""
    today = date.today().isoformat()
    cutoff = (date.today() + timedelta(days=days_ahead)).isoformat()
    rows = []
    for r in run_query("SELECT ticker, asset_name, catalyst_type, expected_date, date_precision "
                       "FROM catalysts WHERE expected_date <= ?", (cutoff,)):
        end = period_end_date(r["expected_date"], r["date_precision"]).isoformat()
        if end >= today:  # the period has not fully passed yet
            rows.append((r["ticker"], r["expected_date"], r["date_precision"], r["catalyst_type"],
                         r["asset_name"]))
    for r in run_query("SELECT ticker, nct_id, title, primary_completion_date FROM trials "
                       "WHERE status IN ('RECRUITING','ACTIVE_NOT_RECRUITING','ENROLLING_BY_INVITATION')"):
        end = r["primary_completion_date"] or ""
        padded = end + "-01" if len(end) == 7 else end  # month-only dates mean the 1st
        if today <= padded <= cutoff:
            precision = "month" if len(end) == 7 else "exact"
            rows.append((r["ticker"], padded, precision, "trial_primary_completion",
                         f"{r['nct_id']} {r['title']}"))
    frame = pd.DataFrame(rows, columns=["ticker", "expected_date", "date_precision", "type", "detail"])
    return frame.sort_values("expected_date").reset_index(drop=True)


# ------------------------------------------------------------- full screen

SCREEN_COLUMNS = [
    "ticker", "cash_m", "burn_per_qtr_m", "runway_months", "market_cap_m", "ev_to_cash", "dilution_1y_pct",
    "dilution_3y_pct", "pct_off_high", "return_30d", "return_90d", "volatility_30d", "runway_flag",
    "catalyst_in_window", "catalyst_weight", "ev_cash_flag"]

# How much an upcoming catalyst should count: big binary events and firm dates count most,
# vague "sometime this year" phase starts count least. Result is 0..1 (best catalyst per ticker).
CATALYST_TYPE_WEIGHT = {"pdufa": 1.0, "topline_data": 1.0, "trial_primary_completion": 0.7, "phase_start": 0.4}
CATALYST_PRECISION_WEIGHT = {"exact": 1.0, "month": 0.9, "quarter": 0.7, "half": 0.4, "year": 0.25}


def catalyst_weights(catalysts):
    """Best catalyst weight (0..1) per ticker from a get_upcoming_catalysts frame."""
    best = {}
    for r in catalysts.to_dict("records"):
        weight = (CATALYST_TYPE_WEIGHT.get(r["type"], 0.6)
                  * CATALYST_PRECISION_WEIGHT.get(r["date_precision"], 0.25))
        best[r["ticker"]] = max(best.get(r["ticker"], 0.0), weight)
    return best


def run_screens(tickers=None):
    """Compute every metric for in-universe tickers; returns one DataFrame with flags."""
    config = load_config()
    if tickers is None:
        tickers = [r["ticker"] for r in run_query(
            "SELECT ticker FROM companies WHERE in_universe = 1 ORDER BY ticker")]
    catalyst_weight = catalyst_weights(get_upcoming_catalysts())
    catalyst_tickers = set(catalyst_weight)
    records = []
    for ticker in tickers:
        runway = get_runway_months(ticker)
        ev_cash = get_ev_to_cash(ticker)
        dilution_1y, dilution_3y = get_dilution_history(ticker)
        context = get_price_context(ticker)
        records.append({
            "ticker": ticker, "cash_m": (get_latest_value(ticker, "cash_and_investments") or 0) / 1e6,
            "burn_per_qtr_m": (get_quarterly_burn(ticker) or 0) / 1e6, "runway_months": runway,
            "market_cap_m": (get_market_cap(ticker) or 0) / 1e6, "ev_to_cash": ev_cash,
            "dilution_1y_pct": dilution_1y, "dilution_3y_pct": dilution_3y,
            "pct_off_high": context.get("pct_off_high"), "return_30d": context.get("return_30d"),
            "return_90d": context.get("return_90d"), "volatility_30d": context.get("volatility_30d"),
            "runway_flag": runway_flag(runway, config),
            "catalyst_in_window": ticker in catalyst_tickers,
            "catalyst_weight": catalyst_weight.get(ticker, 0.0),
            "ev_cash_flag": ev_cash is not None and ev_cash < 1.5,
        })
    return pd.DataFrame(records, columns=SCREEN_COLUMNS)


def snapshot_features(frame, rnpv_ratios=None, snapshot_date=None):
    """Save today's screen outputs per ticker into daily_features (safe to rerun the same day).

    After a year this table is the training set for narrow models; every row only uses
    information available on its date. rnpv_ratios: {ticker: base rNPV/EV ratio}.
    """
    snapshot_date = snapshot_date or date.today().isoformat()
    rnpv_ratios = rnpv_ratios or {}
    rows = []
    for r in frame.to_dict("records"):
        rows.append((r["ticker"], snapshot_date, none_if_nan(r["runway_months"]), none_if_nan(r["ev_to_cash"]),
                     rnpv_ratios.get(r["ticker"]), none_if_nan(r["return_30d"]), none_if_nan(r["return_90d"]),
                     none_if_nan(r["volatility_30d"]), none_if_nan(r["dilution_1y_pct"]),
                     none_if_nan(r["market_cap_m"])))
    conn = get_connection()
    conn.executemany("INSERT OR REPLACE INTO daily_features (ticker, date, runway_months, ev_to_cash, "
                     "rnpv_ratio, price_return_30d, price_return_90d, volatility_30d, dilution_1y_pct, "
                     "market_cap_m) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return len(rows)


def none_if_nan(value):
    """Turn NaN/missing into None so SQLite stores NULL."""
    if value is None or value != value:
        return None
    return float(value)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Print the screens table.")
    parser.add_argument("--tickers", help="comma-separated tickers (default: whole universe)")
    args = parser.parse_args()
    ticker_list = args.tickers.split(",") if args.tickers else None
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 30)
    print(run_screens(ticker_list).round(1).to_string(index=False))
