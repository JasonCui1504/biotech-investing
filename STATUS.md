# Project status (updated 2026-10-09)

Research tool only. It never places trades. Research tool output, not financial advice.

## Done and pushed (branch `claude/magical-brahmagupta-anq6gs`)
| Phase | What | Verified |
|---|---|---|
| 1 | config, db (14 tables), http utils, universe builder, prices | Prices live (XBI, IBB, 3 stocks). Universe pipeline live on a 15-ticker sample. |
| 2 | `edgar.py`, `ctgov.py`, `screens.py` | Live on SMMT, ACAD, ABUS. ABUS cash bug found and fixed (stale XBRL tag). |
| 4 | `rnpv.py`, `event_study.py`, `memo.py` | **Live test passed 2026-10-09**: rNPV with LLM peak-sales suggestions for ACAD/ABUS/KRYS, one cited ACAD memo. Awaiting your review. |
| 3 | `claude_utils.py`, `extract.py`, `classify.py`, `news.py`, theme tagging | **Live smoke test passed 2026-10-09** (details below). Theme tagging not yet run. |

65 tests pass (`python -m pytest`), none need network or keys.

## Universe build: finished 2026-10-09
- 4,459 common stocks -> 4,451 with an SEC CIK -> 567 in biotech SIC codes -> **228 pass the $300M-$10B market cap and $1M/day liquidity filters**.
- Prices loaded: 53,343 rows for 230 tickers (228 companies + XBI + IBB), about a year each.
- Snapshot saved in git: `data/universe_snapshot.csv` (the database itself is not in git).
- Largest: KRYS, PCVX, PTGX, KYMR, AXSM (about $9-10B). Smallest: CYPH, BNTC, PRLD, INDP, SRZN (about $300M).
- Not in the list: SMMT (market cap about $13.7B, above the ceiling, expected) and RVMD (not checked why; look up its market cap or status).
- No themes tagged yet (needs the Claude step: `python -m app.ingest.universe --tag-themes`, after running edgar for filings).
- Some companies may be missing because Yahoo failed or rate-limited (HTTP 429 seen once). Compare against a few names you know.

## Phase 3 live smoke test (2026-10-09, total Claude spend $0.19)
- Pipeline extraction (Sonnet 5.5) for ACAD, ABUS, KRYS: 10 + 2 + 10 assets, $0.18. Spot checks look right (Daybue approved in Rett syndrome; imdusiran Phase 2 in hepatitis B). Still LLM-extracted: verify against 10-Ks before relying on it.
- Cache works: rerunning ABUS extraction made no API call (spend unchanged).
- News classification (Haiku 5.5): 64 headlines in two batches for about $0.01. Tickers assigned only to universe companies.
- 8-K classification worked on 4 filings (e.g. ACAD RADIANT Phase 2 topline = trial_data_mixed, materiality 4).
- Bug fixed: catalysts with period precision (quarter/half/year) were treated as past once the period began; the calendar now compares against the period end.
- Known issue: FierceBiotech and FiercePharma return 403 to this cloud server even with browser headers (IP blocked). Endpoints, STAT and FDA feeds work. Should work from your own computer.
- Not yet tested live: theme tagging, `extract_press_release` on its own, 8-K path on a large batch.

## Phase 4 (2026-10-09, total Claude spend so far about $0.55)
- `rnpv.py`: assumptions listed at the top of the file. Low/base/high = 0.5x/1x/1.5x peak sales. Your numbers in `data/peak_sales_inputs.csv` (ticker, asset_name, peak_sales_usd, source_or_reasoning) override Claude's suggestions; suggestions are stored separately and always labeled "LLM suggestion, unverified". Run: `python -m app.analysis.rnpv --tickers ACAD,KRYS --suggest`.
- `event_study.py`: works but only 3 events so far (n=2 mixed-data events), meaningless until months of history exist.
- `memo.py`: `python -m app.analysis.memo --tickers ACAD`. Memo saved at `reports/memos/` (gitignored). Candidate filter and cap (5/day, `config.yaml` -> `memos`) apply when no tickers are given.
- Known rNPV limitation: approved products use an LLM peak-sales guess, not their actual reported revenue, so a commercial company can look badly undervalued by the model (KRYS: rNPV about $0.6B vs EV about $8.6B). Treat the ratio as a rough screen only. Possible fix: use trailing revenue for approved assets.
- Fact-sheet bug fixed: cash-flow-positive companies now say so instead of "burn $0M".

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

## Next steps (tomorrow)
1. Review the universe list (is it plausible? known names present? ~how many per theme).
2. Run Phase 2 on about 10 real tickers and compare cash and burn with their filings: `python -m app.ingest.edgar --tickers ...`, then `python -m app.analysis.screens --tickers ...`.
3. (done) Live smoke test of Phase 3.
3b. Run theme tagging on the whole universe: `python -m app.ingest.edgar --tickers ALL` then `python -m app.ingest.universe --tag-themes` (est. under $1).
4. Then Phase 4 (rNPV + `peak_sales_inputs.csv`, event study, memos), then Phase 5 (brief, tracking, `run_daily.py`, Resend email, README).

## Risks to remember
- Free-tier Resend only emails the address you signed up with.
- Registry dates (ClinicalTrials.gov) are often stale; XBRL tags vary by company; yfinance can break; LLM output is unverified and labelled as such.
