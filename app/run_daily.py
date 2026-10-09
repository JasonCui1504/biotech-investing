"""Runs the whole daily pipeline in order. Every step is wrapped so one failure never stops the run.

Run:  python -m app.run_daily                   full run
      python -m app.run_daily --skip-claude     everything except Claude steps (zero cost)
      python -m app.run_daily --tickers ACAD,ABUS
      python -m app.run_daily --smoke-test      3 tickers only (ACAD, ABUS, KRYS)
      python -m app.run_daily --dry-run         list the steps without running them
Universe refresh is a separate monthly command: python -m app.ingest.universe --refresh
Scheduling (cron/Task Scheduler) is described in the README; nothing is scheduled automatically.
"""
import argparse
import logging
import time
from datetime import date

from app.config import load_config, project_path, setup_logging
from app.db import create_tables, run_query

log = logging.getLogger(__name__)

SMOKE_TEST_TICKERS = ["ACAD", "ABUS", "KRYS"]
STEP_NAMES = ["create tables", "prices", "filings", "financials", "clinical trials", "news", "classify",
              "extract 10-Ks", "peak sales + screens", "event study", "memos", "tracking", "report"]


def get_universe(tickers_arg):
    """List of (ticker, cik, name) for the run: the given tickers or the whole universe."""
    rows = run_query("SELECT ticker, cik, name FROM companies WHERE in_universe = 1 ORDER BY ticker")
    if tickers_arg:
        wanted = set(tickers_arg)
        rows = [r for r in rows if r["ticker"] in wanted]
    return [(r["ticker"], r["cik"], r["name"]) for r in rows]


def run_step(name, function, health, *args):
    """Run one step, time it, record the result or the error, and keep going on failure."""
    start = time.time()
    try:
        detail = function(*args)
        health["steps"][name] = f"{detail} ({time.time() - start:.0f}s)"
        log.info("STEP %s: %s", name, health["steps"][name])
        return detail
    except Exception as error:  # a failed step must not stop later steps
        log.exception("STEP %s failed", name)
        health["errors"].append(f"{name}: {error}")
        health["steps"][name] = "FAILED"
        return None


# ---------------------------------------------------------------- the steps

def step_prices(universe):
    from app.ingest.prices import update_prices
    config = load_config()
    names = [t for t, _, _ in universe] + [config["benchmark_ticker"]] + config.get("extra_price_tickers", [])
    results = update_prices(names)
    return f"{sum(results.values())} new price rows, {sum(1 for n in results.values() if n == 0)} tickers unchanged"


def get_periodic_accessions():
    """Accession numbers of all stored 10-K/10-Q filings."""
    rows = run_query("SELECT accession_number FROM filings WHERE form_type IN ('10-K','10-Q')")
    return {r["accession_number"] for r in rows}


def step_filings(universe, state):
    from app.ingest.edgar import fetch_recent_filings
    before = get_periodic_accessions()
    new_rows = 0
    for ticker, cik, _ in universe:
        try:
            new_rows += fetch_recent_filings(ticker, cik)
        except Exception as error:
            log.error("%s: filings failed (%s)", ticker, error)
    new_periodic = get_periodic_accessions() - before
    marks = ",".join("?" * len(new_periodic)) or "''"
    rows = run_query(f"SELECT DISTINCT ticker FROM filings WHERE accession_number IN ({marks})", tuple(new_periodic))
    state["tickers_with_new_reports"] = {r["ticker"] for r in rows}
    return f"{new_rows} new filings, {len(new_periodic)} new 10-K/10-Q"


def step_financials(universe, state):
    from app.ingest.edgar import fetch_financials
    have = {r["ticker"] for r in run_query("SELECT DISTINCT ticker FROM financials")}
    todo = [(t, c) for t, c, _ in universe if t in state["tickers_with_new_reports"] or t not in have]
    for ticker, cik in todo:
        try:
            fetch_financials(ticker, cik)
        except Exception as error:
            log.error("%s: financials failed (%s)", ticker, error)
    return f"{len(todo)} tickers refreshed"


def step_ctgov(universe):
    from app.ingest.ctgov import run_ctgov
    with_aliases = {r["ticker"] for r in run_query("SELECT DISTINCT ticker FROM sponsor_aliases")}
    companies = [{"ticker": t, "name": n} for t, _, n in universe]
    new_ones = [c for c in companies if c["ticker"] not in with_aliases]
    if new_ones:  # alias search is slow; only do it for companies we have not matched yet
        run_ctgov(new_ones, refresh_aliases=True)
    summary = run_ctgov(companies, refresh_aliases=False)
    return f"{summary['trials']} trials, {summary['events']} change events"


def step_news():
    from app.ingest.news import fetch_rss_feeds, import_existing_news
    return f"{fetch_rss_feeds()} new RSS items, {import_existing_news()} from biotech-intel"


def step_classify():
    from app.analysis.classify import classify_new_8ks, classify_news_items
    return f"{classify_news_items()} headlines, {classify_new_8ks()} 8-Ks classified"


def step_extract(universe, state):
    from app.analysis.extract import extract_pipeline
    done = []
    for ticker, _, _ in universe:
        try:
            if extract_pipeline(ticker):
                done.append(ticker)
        except Exception as error:
            log.error("%s: extraction failed (%s)", ticker, error)
    state["newly_extracted"] = done
    return f"{len(done)} new 10-Ks extracted"


def step_valuation_and_screens(universe, state, skip_claude):
    from app.analysis.rnpv import load_peak_sales_csv, rnpv_vs_ev, suggest_peak_sales
    from app.analysis.screens import run_screens, snapshot_features
    load_peak_sales_csv()
    if not skip_claude:
        for ticker in state.get("newly_extracted", []):
            suggest_peak_sales(ticker)
    tickers = [t for t, _, _ in universe]
    state["screens"] = run_screens(tickers)
    ratios = {}
    for ticker in tickers:
        result = rnpv_vs_ev(ticker)
        if result and result.get("ratio_base") is not None:
            ratios[ticker] = result["ratio_base"]
    saved = snapshot_features(state["screens"], ratios)
    return f"{len(state['screens'])} companies screened, {saved} feature rows saved"


def step_event_study():
    from app.analysis.event_study import get_event_reactions
    if date.today().weekday() != 0:
        return "skipped (runs on Mondays)"
    return f"{get_event_reactions()} reactions computed"


def step_memos(universe, tickers_given):
    from app.analysis.memo import generate_memo, select_candidates
    from app.analysis.rnpv import suggest_peak_sales
    limit = load_config()["memos"]["max_per_day"]
    chosen = select_candidates(limit, [t for t, _, _ in universe] if tickers_given else None)
    written = 0
    for candidate in chosen:
        suggest_peak_sales(candidate["ticker"])  # idempotent: only fills assets with no number
        outcome = generate_memo(candidate)
        written += 1 if outcome and outcome[1] is not None else 0
    return f"{len(chosen)} candidates, {written} new memos"


def step_tracking():
    from app.tracking.performance import build_performance_text
    return build_performance_text().splitlines()[0]


def step_report(health, skip_claude, tickers):
    from app.report import build_brief, send_brief_email
    path = build_brief(health, skip_claude, tickers)
    sent = send_brief_email(path)
    return f"{path} (emailed: {sent})"


# ---------------------------------------------------------------------- main

def run_all(tickers=None, skip_claude=False, dry_run=False):
    """Run every step in order; returns the health dict."""
    health = {"steps": {}, "errors": []}
    if dry_run:
        print("Dry run. Steps that would run:")
        for number, name in enumerate(STEP_NAMES, start=1):
            skipped = " (skipped: --skip-claude)" if skip_claude and name in ("extract 10-Ks", "memos") else ""
            print(f"  {number}. {name}{skipped}")
        return health
    create_tables()
    universe = get_universe(tickers)
    log.info("Universe for this run: %d companies", len(universe))
    state = {"tickers_with_new_reports": set()}
    run_step("prices", step_prices, health, universe)
    run_step("filings", step_filings, health, universe, state)
    run_step("financials", step_financials, health, universe, state)
    run_step("clinical trials", step_ctgov, health, universe)
    run_step("news", step_news, health)
    if not skip_claude:
        run_step("classify", step_classify, health)
        run_step("extract 10-Ks", step_extract, health, universe, state)
    run_step("peak sales + screens", step_valuation_and_screens, health, universe, state, skip_claude)
    run_step("event study", step_event_study, health)
    if not skip_claude:
        run_step("memos", step_memos, health, universe, bool(tickers))
    run_step("tracking", step_tracking, health)
    run_step("report", step_report, health, health, skip_claude, [t for t, _, _ in universe] if tickers else None)
    return health


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the daily biotech research pipeline.")
    parser.add_argument("--skip-claude", action="store_true", help="skip all steps that call Claude")
    parser.add_argument("--tickers", help="comma-separated tickers to limit the run")
    parser.add_argument("--smoke-test", action="store_true", help="run on 3 tickers only")
    parser.add_argument("--dry-run", action="store_true", help="list steps without running them")
    args = parser.parse_args()
    setup_logging(project_path(f"logs/run_{date.today().isoformat()}.log"))
    ticker_list = SMOKE_TEST_TICKERS if args.smoke_test else (args.tickers.split(",") if args.tickers else None)
    result = run_all(ticker_list, args.skip_claude, args.dry_run)
    if not args.dry_run:
        print("Run complete. Steps:")
        for step_name, step_detail in result["steps"].items():
            print(f"  {step_name}: {step_detail}")
        print(f"Errors: {len(result['errors'])}")
