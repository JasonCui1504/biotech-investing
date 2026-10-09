"""Simple, transparent risk-adjusted NPV (rNPV) of a company's pipeline.

Run:  python -m app.analysis.rnpv --tickers ACAD,KRYS [--suggest]

ASSUMPTIONS (edit these; this is a rough ranking and sanity-check tool, NOT a precise valuation):
  1. Probability of success = chance of reaching approval from the asset's current phase
     (config.yaml -> pos_by_phase; oncology uses lower rates).
  2. Years to launch by phase come from config.yaml -> rnpv.years_to_launch_by_phase.
  3. Profit at peak = peak sales x PROFIT_MARGIN (30%).
  4. Value at launch = annual profit at peak x COMMERCIAL_MULTIPLE (8), a crude stand-in
     for roughly ten years of declining sales after launch.
  5. rNPV = probability x value at launch / (1 + discount rate) ^ years to launch.
  6. Approved assets are NOT valued from guessed peak sales. Instead the company's
     trailing-four-quarter revenue is valued the same way (revenue x margin x multiple,
     probability 1, no discount). A peak-sales number YOU enter for an approved asset
     replaces this for that asset only if you also leave revenue out of it (avoid double counting).
  7. No costs of development, no financing, no competition: those are NOT modeled.
  8. Peak sales is the weakest input. It comes from YOUR csv (data/peak_sales_inputs.csv)
     or, failing that, an unverified Claude suggestion. Reports always show which.
"""
import argparse
import csv
import logging
import os

from app.analysis.classify_prompts import PEAK_SALES_SYSTEM_PROMPT
from app.analysis.screens import get_enterprise_value, get_latest_value, get_market_cap
from app.claude_utils import call_claude_json
from app.config import load_config, project_path, setup_logging
from app.db import create_tables, get_connection, run_query

log = logging.getLogger(__name__)

PROFIT_MARGIN = 0.30
COMMERCIAL_MULTIPLE = 8
CASES = {"low": 0.5, "base": 1.0, "high": 1.5}
ONCOLOGY_WORDS = ["cancer", "tumor", "carcinoma", "lymphoma", "leukemia", "myeloma", "oncology",
                  "sarcoma", "melanoma", "glioma", "neoplasm"]
MAX_ASSETS_PER_COMPANY = 8


# ------------------------------------------------------------ pure calculation

def detect_area(indication, theme):
    """'oncology' if the indication or company theme is oncology, else 'default'."""
    text = (indication or "").lower()
    if any(word in text for word in ONCOLOGY_WORDS):
        return "oncology"
    if theme and theme.startswith("oncology"):
        return "oncology"
    return "default"


def compute_rnpv(assets, discount_rate, pos_table, years_to_launch_table):
    """Return (total_rnpv, breakdown) for a list of assets.

    assets: list of dicts with name, peak_sales_usd, phase, and area ('oncology' or 'default').
    Assets with no peak sales or an unknown phase are skipped.
    """
    total = 0.0
    breakdown = []
    for asset in assets:
        peak_sales = asset.get("peak_sales_usd")
        phase = asset.get("phase")
        if not peak_sales:
            continue
        if phase == "approved":
            probability, years = 1.0, 0
        elif phase in years_to_launch_table:
            probability = pos_table[asset.get("area", "default")][phase]
            years = years_to_launch_table[phase]
        else:
            continue
        annual_profit_at_peak = peak_sales * PROFIT_MARGIN
        value_at_launch = annual_profit_at_peak * COMMERCIAL_MULTIPLE
        value = probability * value_at_launch / (1 + discount_rate) ** years
        total += value
        breakdown.append({"name": asset.get("name"), "phase": phase, "probability": probability,
                          "years_to_launch": years, "value_usd": value,
                          "peak_sales_usd": peak_sales, "peak_sales_source": asset.get("source")})
    return total, breakdown


def compute_commercial_value(trailing_revenue):
    """Value of already-selling products: revenue x margin x multiple (certain, undiscounted)."""
    if not trailing_revenue or trailing_revenue <= 0:
        return 0.0
    return trailing_revenue * PROFIT_MARGIN * COMMERCIAL_MULTIPLE


def get_trailing_revenue(ticker):
    """Sum of revenue over the latest four reported quarters, or None if fewer than four."""
    rows = run_query("SELECT revenue FROM financials WHERE ticker = ? AND revenue IS NOT NULL "
                     "ORDER BY period_end DESC LIMIT 4", (ticker,))
    if len(rows) < 4:
        return None
    return sum(r["revenue"] for r in rows)


# ------------------------------------------------------------- peak-sales inputs

def load_peak_sales_csv():
    """Copy the user's data/peak_sales_inputs.csv into assets (user numbers always win)."""
    path = project_path("data/peak_sales_inputs.csv")
    if not os.path.exists(path):
        return 0
    conn = get_connection()
    updated = 0
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                value = float(row["peak_sales_usd"])
            except (TypeError, ValueError, KeyError):
                continue
            cursor = conn.execute(
                "UPDATE assets SET peak_sales_estimate_usd = ?, peak_sales_source = 'user_csv' "
                "WHERE ticker = ? AND lower(asset_name) = lower(?)",
                (value, row["ticker"].strip(), row["asset_name"].strip()))
            updated += cursor.rowcount
    conn.commit()
    conn.close()
    return updated


def suggest_peak_sales(ticker):
    """Ask Claude for UNVERIFIED peak-sales guesses for assets that have no number yet."""
    rows = run_query(
        "SELECT asset_name, mechanism, indication, phase FROM assets WHERE ticker = ? "
        "AND peak_sales_estimate_usd IS NULL AND peak_sales_claude_suggestion IS NULL "
        "AND phase IN ('phase1','phase2','phase3','filed') LIMIT ?",
        (ticker, MAX_ASSETS_PER_COMPANY))
    if not rows:
        return 0
    lines = "\n".join(f"- {r['asset_name']} | {r['mechanism'] or ''} | {r['indication'] or ''} | {r['phase']}"
                      for r in rows)
    result = call_claude_json(f"peak_sales:{ticker}", load_config()["claude"]["model_smart"],
                              PEAK_SALES_SYSTEM_PROMPT, f"Company ticker: {ticker}\nASSETS:\n{lines}",
                              effort="medium")
    if not isinstance(result, dict):
        return 0
    conn = get_connection()
    saved = 0
    for item in result.get("assets") or []:
        value = item.get("peak_sales_usd")
        if isinstance(value, (int, float)) and value > 0:
            conn.execute("UPDATE assets SET peak_sales_claude_suggestion = ?, "
                         "peak_sales_claude_reasoning = ? WHERE ticker = ? AND asset_name = ?",
                         (value, item.get("reasoning"), ticker, item.get("asset_name")))
            saved += 1
    conn.commit()
    conn.close()
    return saved


# ------------------------------------------------------------- company level

def get_company_assets(ticker):
    """Assets of a ticker as dicts for compute_rnpv; user numbers beat LLM suggestions."""
    theme_rows = run_query("SELECT theme FROM companies WHERE ticker = ?", (ticker,))
    theme = theme_rows[0]["theme"] if theme_rows else None
    assets = []
    for row in run_query("SELECT * FROM assets WHERE ticker = ?", (ticker,)):
        if row["peak_sales_estimate_usd"]:
            peak, source = row["peak_sales_estimate_usd"], "user"
        elif row["peak_sales_claude_suggestion"]:
            peak, source = row["peak_sales_claude_suggestion"], "LLM suggestion, unverified"
        else:
            peak, source = None, None
        if row["phase"] == "approved" and source != "user":
            peak, source = None, None  # valued from reported revenue instead (see compute_commercial_value)
        assets.append({"name": row["asset_name"], "phase": row["phase"], "peak_sales_usd": peak,
                       "source": source, "area": detect_area(row["indication"], theme)})
    return assets


def rnpv_vs_ev(ticker):
    """Compare pipeline rNPV with enterprise value for low/base/high peak-sales cases.

    ratio = rNPV / EV: the market prices the pipeline at about EV (market cap minus net cash),
    so a ratio above 1 means our rough pipeline value exceeds what the market is paying.
    Returns None if there is nothing to value. Always shows a range, never a point estimate.
    """
    config = load_config()
    assets = get_company_assets(ticker)
    ev = get_enterprise_value(ticker)
    market_cap = get_market_cap(ticker)
    cash = get_latest_value(ticker, "cash_and_investments")
    debt = get_latest_value(ticker, "total_debt") or 0
    valued = [a for a in assets if a["peak_sales_usd"]]
    has_approved = any(a["phase"] == "approved" for a in assets)
    commercial = compute_commercial_value(get_trailing_revenue(ticker)) if has_approved else 0.0
    if not valued and not commercial:
        return None
    result = {"ticker": ticker, "ev_usd": ev, "assets_valued": len(valued), "assets_total": len(assets),
              "commercial_value_usd": commercial,
              "llm_share_pct": 100 * sum(a["source"] != "user" for a in valued) / max(len(valued), 1)}
    for case, multiplier in CASES.items():
        scaled = [dict(a, peak_sales_usd=a["peak_sales_usd"] * multiplier) for a in valued]
        total, _ = compute_rnpv(scaled, config["rnpv"]["discount_rate"], config["pos_by_phase"],
                                config["rnpv"]["years_to_launch_by_phase"])
        total += commercial  # reported-revenue value is not scaled by the peak-sales cases
        result[f"rnpv_{case}_usd"] = total
        result[f"ratio_{case}"] = total / ev if ev and ev > 0 else None
        if cash is not None and market_cap:
            result[f"implied_equity_vs_mcap_{case}"] = (total + cash - debt) / market_cap
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compute rNPV vs EV for tickers.")
    parser.add_argument("--tickers", required=True)
    parser.add_argument("--suggest", action="store_true",
                        help="ask Claude for unverified peak-sales suggestions first")
    args = parser.parse_args()
    setup_logging()
    create_tables()
    print(f"Peak sales rows loaded from your CSV: {load_peak_sales_csv()}")
    for item in args.tickers.split(","):
        if args.suggest:
            print(f"{item}: {suggest_peak_sales(item)} LLM suggestions saved")
        out = rnpv_vs_ev(item)
        if out is None:
            print(f"{item}: no assets with peak sales")
            continue
        print(f"{item}: EV ${(out['ev_usd'] or 0) / 1e6:,.0f}M | rNPV low/base/high "
              f"${out['rnpv_low_usd'] / 1e6:,.0f}M / ${out['rnpv_base_usd'] / 1e6:,.0f}M / "
              f"${out['rnpv_high_usd'] / 1e6:,.0f}M | {out['assets_valued']}/{out['assets_total']} "
              f"pipeline assets valued, {out['llm_share_pct']:.0f}% from unverified LLM suggestions; "
              f"marketed products (reported revenue): ${out['commercial_value_usd'] / 1e6:,.0f}M")
