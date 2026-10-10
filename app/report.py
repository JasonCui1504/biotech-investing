"""Builds the daily markdown brief (and emails it through Resend if configured).

Run:  python -m app.report            (writes reports/YYYY-MM-DD_brief.md from current data)
"""
import glob
import html
import logging
import os
import re
from datetime import date, timedelta

import markdown
import pandas as pd
import requests

from app.analysis.memo import parse_verdict
from app.analysis.rnpv import rnpv_vs_ev
from app.analysis.screens import get_upcoming_catalysts, run_screens
from app.claude_utils import call_claude_text, get_todays_spend
from app.config import get_env, load_config, project_path, setup_logging
from app.db import create_tables, run_query
from app.tracking.performance import build_performance_text

log = logging.getLogger(__name__)

FOOTER = "Research tool output, not financial advice."


def format_number(value, digits=1):
    """Fixed-digit number or '-' for missing/NaN."""
    if value is None or value != value:
        return "-"
    return f"{value:.{digits}f}"


def markdown_table(headers, rows):
    """Render a markdown table from headers and a list of row lists."""
    if not rows:
        return "_none_"
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for row in rows:
        lines.append("| " + " | ".join(str(cell).replace("|", "/") for cell in row) + " |")
    return "\n".join(lines)


# ------------------------------------------------------------ watchlist score

def score_company(row, flags, weights):
    """Return (score, components) for one company; components explain every point.

    row: dict from run_screens plus rnpv_base_ratio. flags: dict with negative_trial_change.
    """
    components = []
    if row["catalyst_in_window"]:
        components.append(("catalyst in window", weights["catalyst_in_window"]))
    if row["ev_cash_flag"]:
        components.append(("low EV/cash", weights["low_ev_to_cash"]))
    if row["runway_flag"] == "OK" and row["runway_months"] == row["runway_months"] and row["runway_months"]:
        components.append(("healthy runway", weights["healthy_runway"]))
    ratio = row.get("rnpv_base_ratio")
    if ratio is not None and ratio > 1:
        components.append(("rNPV/EV above 1", weights["rnpv_above_1"]))
    if row["runway_flag"] == "DANGER":
        components.append(("runway under 12 months", weights["runway_danger"]))
    dilution = row.get("dilution_1y_pct")
    if dilution is not None and dilution == dilution and dilution > 20:
        components.append(("large dilution (1y)", weights["large_dilution"]))
    if flags.get("negative_trial_change"):
        components.append(("negative trial change", weights["negative_trial_change"]))
    return sum(points for _, points in components), components


def get_negative_trial_tickers():
    """Tickers with a materiality >= 4 trial-registry change in the last 30 days."""
    since = (date.today() - timedelta(days=30)).isoformat()
    rows = run_query("SELECT DISTINCT ticker FROM events WHERE event_type = 'trial_change' "
                     "AND materiality >= 4 AND event_date >= ?", (since,))
    return {r["ticker"] for r in rows}


def get_latest_verdict(ticker):
    """Verdict from the newest saved memo for a ticker, or '-'."""
    files = sorted(glob.glob(project_path(f"reports/memos/*_{ticker}.md")))
    if not files:
        return "-"
    with open(files[-1]) as f:
        return parse_verdict(f.read()) or "-"


def build_watchlist(screens, top_n=12):
    """Rank companies by score; returns a list of (score, row, components) sorted high to low."""
    weights = load_config()["watchlist_score_weights"]
    negative = get_negative_trial_tickers()
    ranked = []
    for row in screens.to_dict("records"):
        result = rnpv_vs_ev(row["ticker"])
        row["rnpv_base_ratio"] = result["ratio_base"] if result else None
        score, components = score_company(row, {"negative_trial_change": row["ticker"] in negative}, weights)
        ranked.append((score, row, components))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[:top_n]


# ------------------------------------------------------------------- sections

def section_overview(facts, skip_claude):
    """3-5 sentence overview from Claude (fast model), or a plain template."""
    template = (f"{facts['events']} material events and {facts['trial_changes']} trial-registry changes "
                f"were recorded; {facts['catalysts']} catalysts are expected in the next 120 days and "
                f"{facts['danger']} companies have under 12 months of runway.")
    if skip_claude:
        return template
    text = call_claude_text("brief_overview", load_config()["claude"]["model_fast"],
                            "Write a 3-5 sentence overview of today's biotech research brief from these facts. "
                            "Do not add facts.", str(facts), max_tokens=3000, effort="low")
    return text.strip() if text else template


def section_events(since):
    """Material events (materiality >= 3), grouped by ticker."""
    rows = run_query("SELECT * FROM events WHERE materiality >= 3 AND event_type != 'trial_change' "
                     "AND created_at >= ? ORDER BY ticker, materiality DESC", (since,))
    return "\n".join(f"- **{r['ticker']}** ({r['event_date']}, materiality {r['materiality']}): "
                     f"{r['description']}" for r in rows) or "_none_"


def section_trial_changes(today):
    """Today's trial-registry change events."""
    rows = run_query("SELECT * FROM events WHERE event_type = 'trial_change' AND event_date = ? "
                     "ORDER BY materiality DESC", (today,))
    body = "\n".join(f"- **{r['ticker']}** (materiality {r['materiality']}): {r['description']}" for r in rows)
    return (body or "_none_") + "\n\n_Registry dates are sponsor-entered estimates and are often stale: signals, not facts._"


def format_expected(expected_date, precision):
    """Show a catalyst date at the precision it was given (stored dates are period starts)."""
    year, month = expected_date[:4], int(expected_date[5:7])
    if precision == "year":
        return year
    if precision == "half":
        return f"{'1H' if month <= 6 else '2H'} {year}"
    if precision == "quarter":
        return f"Q{(month - 1) // 3 + 1} {year}"
    if precision == "month":
        return expected_date[:7]
    return expected_date


def section_catalysts():
    """Upcoming catalysts table for the next 120 days."""
    frame = get_upcoming_catalysts(120)
    rows = [[format_expected(r["expected_date"], r["date_precision"]), r["date_precision"], r["ticker"],
             r["type"], str(r["detail"])[:70]] for r in frame.head(40).to_dict("records")]
    return markdown_table(["Expected", "Precision", "Ticker", "Type", "Detail"], rows)


def section_watchlist(ranked):
    """Ranking table plus the score components behind each rank."""
    rows = []
    for score, r, components in ranked:
        rows.append([r["ticker"], score, format_number(r["runway_months"]), format_number(r["ev_to_cash"]),
                     format_number(r["rnpv_base_ratio"], 2), get_latest_verdict(r["ticker"])])
    table = markdown_table(["Ticker", "Score", "Runway (mo)", "EV/cash", "rNPV/EV (base)", "Latest memo verdict"], rows)
    why = "\n".join(f"- **{r['ticker']}** {score:+d}: " + (", ".join(f"{name} ({points:+d})"
                    for name, points in components) or "no scoring factors") for score, r, components in ranked)
    return (table + "\n\n_rNPV/EV rests on unverified LLM peak-sales suggestions unless you supplied the numbers._"
            "\n\nScore components:\n" + why)


def section_financing(screens):
    """Companies with runway under 12 months or a recent dilutive financing."""
    since = (date.today() - timedelta(days=30)).isoformat()
    financed = {r["ticker"] for r in run_query(
        "SELECT ticker FROM events WHERE description LIKE '[financing_dilutive]%' AND event_date >= ?", (since,))}
    rows = []
    for r in screens.to_dict("records"):
        if r["runway_flag"] == "DANGER" or r["ticker"] in financed:
            rows.append([r["ticker"], format_number(r["runway_months"]), format_number(r["dilution_1y_pct"]),
                         "yes" if r["ticker"] in financed else "no"])
    return markdown_table(["Ticker", "Runway (mo)", "Share count growth 1y %", "Dilutive financing (30d)"], rows)


def section_memos(today):
    """Links to memo files written today."""
    files = sorted(glob.glob(project_path(f"reports/memos/{today}_*.md")))
    return "\n".join(f"- [{os.path.basename(f)}](memos/{os.path.basename(f)})" for f in files) or "_none today_"


def section_health(health):
    """Pipeline health: what ran, what failed, and Claude spend."""
    lines = [f"- {step}: {detail}" for step, detail in health.get("steps", {}).items()]
    errors = health.get("errors", [])
    lines.append(f"- Errors: {len(errors)}" + ("".join(f"\n  - {e}" for e in errors[:10])))
    lines.append(f"- Claude spend today: ${get_todays_spend():.2f} of "
                 f"${load_config()['claude']['daily_budget_usd']:.2f} budget")
    return "\n".join(lines)


# ----------------------------------------------------------------------- main

def build_brief(health=None, skip_claude=False, tickers=None):
    """Write reports/{date}_brief.md and return its path."""
    health = health or {}
    today = date.today().isoformat()
    since = (date.today() - timedelta(days=1)).isoformat()
    screens = run_screens(tickers)
    ranked = build_watchlist(screens)
    facts = {"events": len(run_query("SELECT 1 FROM events WHERE materiality >= 3 AND created_at >= ?", (since,))),
             "trial_changes": len(run_query("SELECT 1 FROM events WHERE event_type='trial_change' AND event_date = ?", (today,))),
             "catalysts": len(get_upcoming_catalysts(120)), "danger": int((screens["runway_flag"] == "DANGER").sum())}
    parts = [f"# Biotech research brief, {today}", "", "## 1. Overview", section_overview(facts, skip_claude),
             "", "## 2. Material changes (last 24h)", section_events(since),
             "", "## 3. Trial registry changes", section_trial_changes(today),
             "", "## 4. Upcoming catalysts (next 120 days)", section_catalysts(),
             "", "## 5. Watchlist ranking", section_watchlist(ranked),
             "", "## 6. Financing and dilution watch", section_financing(screens),
             "", "## 7. New memos", section_memos(today),
             "", "## 8. Paper portfolio and performance vs XBI", build_performance_text(),
             "", "## 9. Pipeline health", section_health(health),
             "", "---", FOOTER, ""]
    path = project_path(f"reports/{today}_brief.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("\n".join(parts))
    return path


# Mail clients drop <style> blocks, so styles are inlined onto each tag.
EMAIL_STYLES = {
    "h1": "font-size:22px;margin:0 0 16px;color:#111;",
    "h2": "font-size:17px;margin:28px 0 8px;padding-bottom:4px;border-bottom:1px solid #ddd;color:#111;",
    "p": "margin:0 0 12px;",
    "ul": "margin:0 0 12px;padding-left:22px;",
    "li": "margin:0 0 4px;",
    "table": "border-collapse:collapse;width:100%;margin:0 0 12px;font-size:13px;",
    "th": "text-align:left;padding:6px 8px;background:#f2f4f7;border:1px solid #ddd;white-space:nowrap;",
    "td": "padding:6px 8px;border:1px solid #ddd;vertical-align:top;",
    "a": "color:#1a5fb4;",
    "hr": "border:0;border-top:1px solid #ddd;margin:24px 0 12px;",
}


def brief_to_html(text):
    """Convert the markdown brief to an inline-styled HTML email body."""
    body = markdown.markdown(html.escape(text, quote=False), extensions=["tables"])
    for tag, style in EMAIL_STYLES.items():
        body = re.sub(rf"<{tag}(?=[\s>/])", f'<{tag} style="{style}"', body)
    return ('<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;font-size:14px;'
            f'line-height:1.5;color:#222;max-width:760px;margin:0 auto;padding:16px;">{body}</div>')


def send_brief_email(path):
    """Email the brief through Resend's HTTP API; returns True if sent. Needs the 3 env vars."""
    api_key, sender, recipient = get_env("RESEND_API_KEY"), get_env("EMAIL_FROM"), get_env("EMAIL_TO")
    if not (api_key and sender and recipient):
        log.info("Email not configured (RESEND_API_KEY / EMAIL_FROM / EMAIL_TO); skipping")
        return False
    with open(path) as f:
        body = f.read()
    try:
        response = requests.post("https://api.resend.com/emails", timeout=30,
                                 headers={"Authorization": f"Bearer {api_key}"},
                                 json={"from": sender, "to": [recipient],
                                       "subject": f"Biotech brief {date.today().isoformat()}",
                                       "html": brief_to_html(body), "text": body})
        response.raise_for_status()
        return True
    except requests.RequestException as error:
        log.error("Email failed: %s", error)
        return False


if __name__ == "__main__":
    setup_logging()
    create_tables()
    brief_path = build_brief(skip_claude=True)
    print(f"Brief written to {brief_path}")
