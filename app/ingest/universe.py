"""Builds the company universe: US-listed biotech stocks in the market-cap range.

Run monthly:  python -m app.ingest.universe --refresh
Steps: symbol lists -> CIK map -> SIC filter -> market cap / liquidity filter -> save.
(Theme tagging with Claude is added in Phase 3.)
"""
import argparse
import json
import logging
import os
from datetime import date

from app.config import load_config, project_path, setup_logging
from app.db import create_tables, get_connection
from app.http_utils import get_json, get_text

log = logging.getLogger(__name__)

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"

# Words in a security name that mean "not plain common stock".
BAD_NAME_WORDS = ["warrant", "unit", "right", "preferred", "note", "depositary",
                  "debenture", "trust", "etf", "fund"]
# otherlisted.txt uses one-letter exchange codes. N = NYSE.
OTHER_EXCHANGE_CODES = {"N": "NYSE"}


def parse_pipe_file(text):
    """Turn a pipe-delimited text file into a list of dicts (header row gives the keys)."""
    lines = text.strip().splitlines()
    header = lines[0].split("|")
    rows = []
    for line in lines[1:]:
        if line.startswith("File Creation Time"):
            continue
        parts = line.split("|")
        if len(parts) == len(header):
            rows.append(dict(zip(header, parts)))
    return rows


def is_plain_common_stock(name, ticker):
    """True if the security name and ticker look like ordinary common stock."""
    lower_name = name.lower()
    if "$" in ticker or "." in ticker:  # preferred shares and share classes
        return False
    for word in BAD_NAME_WORDS:
        if word in lower_name:
            return False
    return "common stock" in lower_name or "ordinary shares" in lower_name


def load_symbol_lists():
    """Download both Nasdaq Trader files; return {ticker: {'name':..., 'exchange':...}}."""
    symbols = {}
    nasdaq_text = get_text(NASDAQ_LISTED_URL)
    if nasdaq_text:
        for row in parse_pipe_file(nasdaq_text):
            if row["Test Issue"] == "Y" or row["ETF"] == "Y":
                continue
            if is_plain_common_stock(row["Security Name"], row["Symbol"]):
                symbols[row["Symbol"]] = {"name": row["Security Name"], "exchange": "NASDAQ"}
    other_text = get_text(OTHER_LISTED_URL)
    if other_text:
        for row in parse_pipe_file(other_text):
            exchange = OTHER_EXCHANGE_CODES.get(row["Exchange"])
            if exchange is None or row["Test Issue"] == "Y" or row["ETF"] == "Y":
                continue
            ticker = row["ACT Symbol"]
            if is_plain_common_stock(row["Security Name"], ticker):
                symbols[ticker] = {"name": row["Security Name"], "exchange": exchange}
    return symbols


def load_cik_map():
    """Download SEC's ticker list; return {ticker: 10-digit zero-padded CIK string}."""
    data = get_json(SEC_TICKERS_URL)
    cik_map = {}
    if data is None:
        return cik_map
    for entry in data.values():
        cik_map[entry["ticker"]] = str(entry["cik_str"]).zfill(10)
    return cik_map


def load_sic_cache():
    """Read data/sic_cache.json ({cik: sic}); SIC codes almost never change."""
    path = project_path("data/sic_cache.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def save_sic_cache(cache):
    """Write the SIC cache back to disk."""
    with open(project_path("data/sic_cache.json"), "w") as f:
        json.dump(cache, f)


def get_sic_code(cik, cache):
    """Return the SIC code for a CIK (from cache or SEC); None if it can't be found."""
    if cik in cache:
        return cache[cik]
    data = get_json(SEC_SUBMISSIONS_URL.format(cik=cik), pause_seconds=0.15)
    if data is None:
        return None
    try:
        sic = int(data.get("sic"))
    except (TypeError, ValueError):
        sic = 0
    cache[cik] = sic
    return sic


def get_market_data(ticker):
    """Return (market_cap, avg_daily_dollar_volume) from yfinance, or (None, None)."""
    import yfinance as yf
    try:
        info = yf.Ticker(ticker).fast_info
        market_cap = info["marketCap"]
        # Dollar volume = shares traded per day * price; a liquidity measure.
        dollar_volume = info["threeMonthAverageVolume"] * info["lastPrice"]
        return market_cap, dollar_volume
    except Exception as error:
        log.warning("%s: no market data (%s)", ticker, error)
        return None, None


def save_company(conn, ticker, info, cik, sic, market_cap, dollar_volume):
    """Insert or update one company row without wiping its theme/modality tags."""
    conn.execute(
        """INSERT INTO companies (ticker, name, cik, exchange, sic_code, market_cap_usd,
                                  avg_daily_dollar_volume, last_updated)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(ticker) DO UPDATE SET
               name=excluded.name, cik=excluded.cik, exchange=excluded.exchange,
               sic_code=excluded.sic_code, market_cap_usd=excluded.market_cap_usd,
               avg_daily_dollar_volume=excluded.avg_daily_dollar_volume,
               last_updated=excluded.last_updated""",
        (ticker, info["name"], cik, info["exchange"], sic, market_cap, dollar_volume,
         date.today().isoformat()))


def refresh_universe(max_tickers=None):
    """Run the whole pipeline and save survivors to the companies table."""
    config = load_config()["universe"]
    create_tables()

    symbols = load_symbol_lists()
    print(f"1. Common stocks on {config['exchanges']}: {len(symbols)}")

    cik_map = load_cik_map()
    candidates = [t for t in symbols if t in cik_map]
    print(f"2. With an SEC CIK: {len(candidates)}")

    sic_cache = load_sic_cache()
    biotech = []
    for number, ticker in enumerate(candidates, start=1):
        if max_tickers and len(biotech) >= max_tickers:
            break
        if number % 200 == 0:
            print(f"   ...checked SIC for {number}/{len(candidates)}")
            save_sic_cache(sic_cache)
        sic = get_sic_code(cik_map[ticker], sic_cache)
        if sic in config["sic_codes"]:
            biotech.append((ticker, sic))
    save_sic_cache(sic_cache)
    print(f"3. In SIC codes {config['sic_codes']}: {len(biotech)}")

    conn = get_connection()
    kept = 0
    for ticker, sic in biotech:
        market_cap, dollar_volume = get_market_data(ticker)
        if market_cap is None:
            continue
        if not config["market_cap_min_usd"] <= market_cap <= config["market_cap_max_usd"]:
            continue
        if dollar_volume < config["min_avg_daily_dollar_volume"]:
            continue
        save_company(conn, ticker, symbols[ticker], cik_map[ticker], sic, market_cap, dollar_volume)
        kept += 1
    conn.commit()
    conn.close()
    print(f"4. Passed market cap + liquidity filters and saved: {kept}")
    print_summary()


def print_summary():
    """Print how many companies are in the table, split by theme."""
    conn = get_connection()
    total = conn.execute("SELECT COUNT(*) FROM companies WHERE in_universe=1").fetchone()[0]
    print(f"\nUniverse size: {total}")
    rows = conn.execute("""SELECT COALESCE(theme, '(not tagged yet)') AS theme, COUNT(*) AS n
                           FROM companies WHERE in_universe=1 GROUP BY theme""").fetchall()
    for row in rows:
        print(f"  {row['theme']}: {row['n']}")
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build or refresh the biotech universe.")
    parser.add_argument("--refresh", action="store_true", help="rebuild the universe")
    parser.add_argument("--max-tickers", type=int, default=None,
                        help="stop after this many SIC-matching tickers (for quick tests)")
    args = parser.parse_args()
    setup_logging()
    if args.refresh:
        refresh_universe(args.max_tickers)
    else:
        parser.print_help()
        print_summary()
