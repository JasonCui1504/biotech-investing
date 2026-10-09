# Biotech Research & Paper-Trading System

Research tool only. It never places trades. See `BUILD_SPEC.md` for the full plan.
**Research tool output, not financial advice.**

## Setup
```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # then fill in SEC_USER_AGENT ("Your Name you@example.com")
```

## Phase 1 commands
```
python -m app.db                          # create data/biotech.db
python -m app.ingest.universe --refresh   # build the universe (monthly)
python -m app.ingest.prices               # prices for universe + XBI + IBB
python -m pytest                          # tests (no network needed)
```
Themes live in `config.yaml` under `universe.themes`.

(Full README is written in Phase 5.)
