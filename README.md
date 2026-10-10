# Biotech Research & Paper-Trading System

A daily Python pipeline that tracks small/mid-cap US biotech stocks, reads SEC filings, clinical trial
registry changes and news, values pipelines with a simple rNPV, writes cited thesis memos with Claude,
and logs paper recommendations against the XBI benchmark.

**It never places real trades. It produces research and a paper-trading log only.**
**Research tool output, not financial advice.**

## Setup

```
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                 # then edit .env
python -m pytest                     # tests need no network or API keys
```

What goes in `.env` (never commit this file; it is gitignored):

| Variable | What it is |
|---|---|
| `SEC_USER_AGENT` | Your name and email, e.g. `Jane Doe jane@example.com`. SEC requires it. Not a secret, not a signup. |
| `BIOTECH_ANTHROPIC_API_KEY` (or `ANTHROPIC_API_KEY`) | Anthropic API key from console.anthropic.com. The first name is checked first. |
| `RESEND_API_KEY`, `EMAIL_FROM`, `EMAIL_TO` | Optional, for the emailed brief (resend.com). On the free tier, `EMAIL_FROM=onboarding@resend.dev` and `EMAIL_TO` must be the address you signed up with. |

All other settings (themes, models, thresholds, budget) live in `config.yaml`.

## Commands

| What | Command |
|---|---|
| Build or refresh the company universe (monthly, ~15-30 min) | `python -m app.ingest.universe --refresh` |
| Load filings and financials for the universe | `python -m app.ingest.edgar --tickers ALL` |
| Tag companies with your themes (Claude, ~$0.45 for 200 companies) | `python -m app.ingest.universe --tag-themes` |
| Daily run (everything) | `python -m app.run_daily` |
| Daily run without any Claude calls (free) | `python -m app.run_daily --skip-claude` |
| Quick test on 3 tickers | `python -m app.run_daily --smoke-test` |
| Limit to some tickers | `python -m app.run_daily --tickers ACAD,ABUS` |
| List the steps without running | `python -m app.run_daily --dry-run` |
| Screens table only | `python -m app.analysis.screens --tickers ACAD,ABUS` |
| rNPV vs EV | `python -m app.analysis.rnpv --tickers ACAD --suggest` |
| Thesis memo | `python -m app.analysis.memo --tickers ACAD` |
| Event study | `python -m app.analysis.event_study` |
| Performance of paper picks | `python -m app.tracking.performance` |

Outputs: the database `data/biotech.db`, daily briefs `reports/YYYY-MM-DD_brief.md`, memos
`reports/memos/`, and logs `logs/run_YYYY-MM-DD.log`. The daily run is idempotent: running it twice on
the same day does not duplicate rows (tested: every table's row count stayed the same).

### Daily run order

prices -> SEC filings -> financials (only tickers with a new 10-K/10-Q) -> ClinicalTrials.gov snapshots and
diffs -> RSS news -> classify headlines and 8-Ks -> extract new 10-Ks -> peak sales + screens ->
event study (Mondays) -> memos for candidates -> performance -> brief (+ email).
Each step is wrapped so a failure is logged and the run continues.

## Editing themes

In `config.yaml`, under `universe.themes`, edit or add `name` and `description`. After changing themes, clear
old tags (`UPDATE companies SET theme = NULL`) and rerun `--tag-themes`. Companies that fit no theme are tagged
`none` and dropped from the universe; companies with no 10-K on file (e.g. recent IPOs) stay untagged.

## Peak sales and rNPV

`data/peak_sales_inputs.csv` has the columns `ticker,asset_name,peak_sales_usd,source_or_reasoning`.
Add a row for any asset where you have your own market estimate; asset names must match the `assets` table.
Your numbers override Claude's suggestions. Where you give none, Claude proposes a base-case peak sales number
with its reasoning (saved in `peak_sales_claude_suggestion`) and every report labels it
**"LLM suggestion, unverified"**. The model shows a low/base/high range (0.5x, 1x, 1.5x), never a point value.

How rNPV is computed (also at the top of `app/analysis/rnpv.py`): probability of approval from the asset's
phase x (peak sales x 30% margin x 8, a crude multiple for ~10 commercial years) discounted at 12% for the years
to launch. Marketed products are valued from trailing-four-quarter reported revenue the same way. It ignores
development costs, financing and competition. **It is a rough ranking and sanity-check tool, not a valuation.**
The 8x-profit multiple is conservative for fast-growing commercial companies, who can look "expensive" on it.

Phase-to-approval probabilities in `config.yaml` (`pos_by_phase`) are approximate industry base rates. They are
consistent with the published BIO / Informa Pharma Intelligence / QLS "Clinical Development Success Rates
2011-2020" report (about 7.9% from Phase 1 overall and about 5% in oncology; about 58% Phase 3 to approval),
as I recall them. I have not re-verified those figures in this project, so check the report and edit the numbers.

## Cost

SEC EDGAR, ClinicalTrials.gov and yfinance are free. Claude costs, measured on real runs (prices as of
2026-10-06: Haiku 5.5 $0.10/$0.50, Sonnet 5.5 $2/$10 per million tokens; recheck `PRICES` in `app/claude_utils.py`):

| Task | Approx. cost |
|---|---|
| Theme tag, per company (Haiku) | $0.002 |
| 10-K pipeline extraction, per company (Sonnet) | $0.06 |
| Peak-sales suggestions, per company (Sonnet) | $0.007 |
| Thesis memo (Sonnet) | $0.02 |
| 100 news headlines / one 8-K | ~$0.01 / ~$0.0004 |

A normal day (a few headlines, a few 8-Ks, up to 5 memos) is typically well under $0.50. The one expensive
event is the first extraction of every company's 10-K (about 130 companies x $0.06 = $8), which the
`daily_budget_usd` cap ($5) spreads over two days; unprocessed filings simply continue the next day.
Results are cached, so the same document is never paid for twice. Prompt caching is not used yet.

## Scheduling

GitHub Actions runs the daily brief automatically (`.github/workflows/daily-brief.yml`, weekdays 7am US Eastern). Required repo secrets: `ANTHROPIC_API_KEY`, `SEC_USER_AGENT`, `RESEND_API_KEY`, `EMAIL_FROM`, `EMAIL_TO`. The database is kept between runs in the Actions cache. The manual options below are only needed to run it elsewhere.

### Manual (cron / Task Scheduler)

macOS/Linux cron (weekdays 7:00; edit paths):
```
0 7 * * 1-5 cd /path/to/biotech-investing && .venv/bin/python -m app.run_daily >> logs/cron.log 2>&1
```
Windows: Task Scheduler -> Create Task -> trigger daily -> action: start program
`C:\path\.venv\Scripts\python.exe` with arguments `-m app.run_daily` and "Start in" your project folder.
Run it on a computer that stays on and keeps `data/` between runs; a temporary cloud container will not.

## Known limitations

- **Registry dates are stale.** ClinicalTrials.gov dates are sponsor-entered estimates; changes are signals to investigate, not facts.
- **yfinance is unofficial** and can rate-limit or break (market caps for a few companies may be missing). Fallbacks (Stooq, Tiingo) are not implemented.
- **XBRL tags vary by company.** The code tries several tags per metric per period, but unusual filers can still show wrong cash, burn or share counts. Spot-check against the filing.
- **LLM extraction can be wrong.** Pipelines, catalysts, themes, peak sales and memos are unverified; they are labeled as such in the database and reports.
- **rNPV is crude** and its peak-sales input is the weakest part (see above).
- **Some feeds block cloud IPs** (FierceBiotech and FiercePharma returned 403 from a cloud server).
- **Event study** needs months of history; sample sizes are printed next to every statistic.
- **Form 4 insider buying** is not parsed yet (its weight in the score is 0).
- **The biotech-intel adapter is off** until that tool has a database (`news.existing_news_db_path`).
- **Survivorship bias:** delisted failures vanish from free datasets, so past results look better than reality.

## Toward models later (not built)

The `daily_features` table saves each day's screen outputs per ticker, using only information available on that
date. After 12+ months it can train narrow models (e.g. reaction to catalysts) with scikit-learn, using
time-based splits only. Avoid look-ahead bias (using data not yet public), survivorship bias and tiny samples.
Price-prediction ML is deliberately not part of this project.

## Disclaimer

Paper trading and research only. Nothing here is investment advice, and no real orders are ever placed.
