# Project status (updated 2026-10-09)

Research tool only. It never places trades. Research tool output, not financial advice.

## Done and pushed (branch `claude/magical-brahmagupta-anq6gs`)
| Phase | What | Verified |
|---|---|---|
| 1 | config, db (16 tables), http utils, universe, prices | Live: full universe built, 1 year of prices. |
| 2 | `edgar.py`, `ctgov.py`, `screens.py` | Live on many tickers; ABUS cash bug found and fixed. |
| 3 | `claude_utils.py`, `extract.py`, `classify.py`, `news.py`, theme tagging | Live smoke test passed; themes tagged. |
| 4 | `rnpv.py`, `event_study.py`, `memo.py` | Live: rNPV, three cited memos. |
| 5 | `report.py`, `tracking/`, `run_daily.py`, email (Resend), README, `daily_features` | **Smoke test passed twice, 0 errors, second run added no duplicate rows.** Full-universe run not done yet. |

76 tests pass (`python -m pytest`), none need network or keys. Claude spend so far today: about $0.80.

## Universe (2026-10-09)
- 228 passed the market-cap filter; after theme tagging **137 stay in the universe**: 49 oncology (bispecifics/ADC/radiopharma), 37 genetic medicine, 26 neuropsychiatry, 25 untagged (no 10-K on file, e.g. recent IPOs). 91 were tagged `none` and dropped.
- Saved list with tags: `data/universe_snapshot.csv` (the database itself is not in git).
- SMMT is above the $10B ceiling; RVMD not checked.

## Phase 5 notes
- Daily run: `python -m app.run_daily` (flags: `--skip-claude`, `--tickers`, `--smoke-test`, `--dry-run`). Order and costs are in README.md.
- Email goes through Resend if `RESEND_API_KEY`, `EMAIL_FROM`, `EMAIL_TO` are set; otherwise it is skipped with a log line. Not tested live.
- The first full extraction of all 10-Ks would cost about $8, so the $5/day budget cap will spread it over two days.
- Bugs fixed during testing: report step missing an argument; SQLite "database is locked" under concurrent runs (connections now wait 60s); theme tagging loop now survives single failures.
- Approved products are now valued from trailing reported revenue instead of LLM peak sales.

## Setup facts
- SEC contact: `Jason Cui jasoncui1504@gmail.com` (in `.env` in the cloud container; set as an environment variable for new sessions).
- Anthropic key: set as `BIOTECH_ANTHROPIC_API_KEY` in the environment settings. Verified valid (HTTP 200) from a separate session. The code reads that name first, then `ANTHROPIC_API_KEY`.
- Resend (Phase 5): `EMAIL_FROM=onboarding@resend.dev`, `EMAIL_TO` = the email you signed up to Resend with.
- Your local `.env` and the cloud `.env` are separate files.

## Decisions and deviations to review
- Themes: oncology (bispecifics/ADC/radiopharma), neuropsychiatry, genetic medicine. The last two descriptions are my wording; edit in `config.yaml`.
- Quarters stored as Q1-Q4 (Q4 = full year minus 9 months), not the spec's "FY" row.
- `diff_snapshots` returns `(description, materiality)` pairs, not plain strings.
- `max_tokens_default` raised from 2000 to 8000 (reasoning tokens count against it).
- No server-side refusal fallbacks: a refused Claude call returns `None` and is skipped.
- biotech-intel (your news tool) has no database yet, so the adapter is off (`existing_news_db_path: null`). Its five RSS feeds are copied into `config.yaml`.
- Model prices in `claude_utils.py` come from the Claude API docs as of 2026-10-06. Recheck before trusting cost numbers.

## Next steps
1. Review the brief (`reports/YYYY-MM-DD_brief.md`) and memos with a skeptical eye; compare numbers with real filings.
2. Run the full universe once without Claude: `python -m app.run_daily --skip-claude` (free, 20-40 min), then with Claude.
3. Put your own peak-sales estimates in `data/peak_sales_inputs.csv` for companies you care about.
4. Decide where it runs daily (your own computer is better than a temporary cloud container) and set up cron/Task Scheduler.
5. Optional later: Form 4 insider parsing, biotech-intel adapter once it has a database, Stooq fallback for prices.

## Risks to remember
- Free-tier Resend only emails the address you signed up with.
- Registry dates (ClinicalTrials.gov) are often stale; XBRL tags vary by company; yfinance can break; LLM output is unverified and labelled as such.
