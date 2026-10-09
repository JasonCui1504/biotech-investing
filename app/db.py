"""SQLite helpers: connection, table creation, and two tiny query functions."""
import os
import sqlite3

from app.config import load_config, project_path

# ---------------------------------------------------------------------------
# Table definitions (plain SQL strings). Every table uses IF NOT EXISTS so
# create_tables() is safe to run on every start.
# ---------------------------------------------------------------------------

CREATE_COMPANIES = """
CREATE TABLE IF NOT EXISTS companies (
    ticker TEXT PRIMARY KEY,
    name TEXT,
    cik TEXT,                    -- SEC ID, zero-padded to 10 digits
    exchange TEXT,
    sic_code INTEGER,
    market_cap_usd REAL,
    avg_daily_dollar_volume REAL,
    theme TEXT,                  -- from config themes, filled by Claude tagging
    modality TEXT,               -- e.g. bispecific, ADC, small molecule
    lead_asset TEXT,
    lead_phase TEXT,
    in_universe INTEGER DEFAULT 1,
    last_updated TEXT
)"""

CREATE_SPONSOR_ALIASES = """
CREATE TABLE IF NOT EXISTS sponsor_aliases (
    alias TEXT PRIMARY KEY,
    ticker TEXT,
    match_method TEXT            -- 'exact', 'fuzzy', 'manual'
)"""

CREATE_ASSETS = """
CREATE TABLE IF NOT EXISTS assets (
    asset_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT,
    asset_name TEXT,
    mechanism TEXT,
    indication TEXT,
    phase TEXT,                  -- preclinical, phase1, phase2, phase3, filed, approved
    peak_sales_estimate_usd REAL,   -- NULL unless company or user supplies it
    source_doc TEXT,
    extracted_at TEXT,
    UNIQUE(ticker, asset_name, indication)
)"""

CREATE_TRIALS = """
CREATE TABLE IF NOT EXISTS trials (
    nct_id TEXT PRIMARY KEY,
    ticker TEXT,
    title TEXT,
    phase TEXT,
    status TEXT,
    enrollment INTEGER,
    primary_endpoint TEXT,
    primary_completion_date TEXT,
    last_update_posted TEXT,
    last_fetched TEXT
)"""

CREATE_TRIAL_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS trial_snapshots (
    nct_id TEXT,
    snapshot_date TEXT,
    status TEXT,
    enrollment INTEGER,
    primary_endpoint TEXT,
    primary_completion_date TEXT,
    PRIMARY KEY (nct_id, snapshot_date)
)"""

CREATE_FILINGS = """
CREATE TABLE IF NOT EXISTS filings (
    accession_number TEXT PRIMARY KEY,
    ticker TEXT,
    form_type TEXT,              -- 10-K, 10-Q, 8-K, 4
    filing_date TEXT,
    primary_document_url TEXT,
    processed INTEGER DEFAULT 0  -- 1 once Claude has extracted/classified it
)"""

CREATE_FINANCIALS = """
CREATE TABLE IF NOT EXISTS financials (
    ticker TEXT,
    period_end TEXT,
    fiscal_period TEXT,          -- Q1, Q2, Q3, FY
    cash_and_investments REAL,
    operating_cash_flow REAL,
    net_income REAL,
    revenue REAL,
    shares_outstanding REAL,
    total_debt REAL,
    filed_date TEXT,             -- when this became public (avoids look-ahead bias)
    PRIMARY KEY (ticker, period_end, fiscal_period)
)"""

CREATE_PRICES = """
CREATE TABLE IF NOT EXISTS prices (
    ticker TEXT,
    date TEXT,
    open REAL, high REAL, low REAL, close REAL, adj_close REAL, volume REAL,
    PRIMARY KEY (ticker, date)
)"""

CREATE_NEWS_ITEMS = """
CREATE TABLE IF NOT EXISTS news_items (
    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE,
    title TEXT,
    source TEXT,
    published_at TEXT,
    fetched_at TEXT,
    snippet TEXT,                -- headline plus short summary only; do NOT store paywalled full text
    ticker TEXT,                 -- filled by classifier (may be NULL)
    event_type TEXT,             -- filled by classifier
    materiality INTEGER,         -- 1-5, filled by classifier
    classified INTEGER DEFAULT 0
)"""

CREATE_EVENTS = """
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT,
    event_date TEXT,
    event_type TEXT,             -- trial_change, filing_8k, news, catalyst_upcoming, financing, fda_action
    description TEXT,
    materiality INTEGER,
    source_ref TEXT,             -- nct_id, accession_number, or news url
    created_at TEXT,
    UNIQUE(ticker, event_type, source_ref, event_date)
)"""

CREATE_CATALYSTS = """
CREATE TABLE IF NOT EXISTS catalysts (
    catalyst_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT,
    asset_name TEXT,
    catalyst_type TEXT,          -- topline_data, pdufa, adcom, phase_start
    expected_date TEXT,          -- ISO date, or the first day of the quarter if only a quarter is given
    date_precision TEXT,         -- exact, month, quarter, half, year
    source TEXT,
    source_url TEXT,
    last_confirmed TEXT,
    UNIQUE(ticker, asset_name, catalyst_type, expected_date)
)"""

CREATE_CLAUDE_CACHE = """
CREATE TABLE IF NOT EXISTS claude_cache (
    cache_key TEXT PRIMARY KEY,  -- hash of (task_name + model + input text)
    response_json TEXT,
    created_at TEXT
)"""

CREATE_CLAUDE_USAGE = """
CREATE TABLE IF NOT EXISTS claude_usage (
    usage_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT,
    task_name TEXT,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    est_cost_usd REAL
)"""

CREATE_RECOMMENDATIONS = """
CREATE TABLE IF NOT EXISTS recommendations (
    rec_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT,
    rec_date TEXT,
    action TEXT,                 -- WATCH, BUY_PAPER, TRIM_PAPER, SELL_PAPER, AVOID
    entry_price REAL,
    rationale TEXT,
    bull_case TEXT,
    bear_case TEXT,
    key_catalyst TEXT,
    target_review_date TEXT,
    exit_date TEXT,
    exit_price REAL,
    status TEXT DEFAULT 'open'   -- open, closed
)"""

ALL_TABLES = [
    CREATE_COMPANIES, CREATE_SPONSOR_ALIASES, CREATE_ASSETS, CREATE_TRIALS,
    CREATE_TRIAL_SNAPSHOTS, CREATE_FILINGS, CREATE_FINANCIALS, CREATE_PRICES,
    CREATE_NEWS_ITEMS, CREATE_EVENTS, CREATE_CATALYSTS, CREATE_CLAUDE_CACHE,
    CREATE_CLAUDE_USAGE, CREATE_RECOMMENDATIONS,
]


def get_db_path():
    """Return the absolute path of the database file named in config.yaml."""
    return project_path(load_config()["database_path"])


def get_connection(db_path=None):
    """Open the SQLite database (creating its folder if needed); rows act like dicts."""
    if db_path is None:
        db_path = get_db_path()
    folder = os.path.dirname(db_path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def create_tables(db_path=None):
    """Create every table if it does not exist yet. Safe to call repeatedly."""
    conn = get_connection(db_path)
    for sql in ALL_TABLES:
        conn.execute(sql)
    conn.commit()
    conn.close()


def run_query(sql, params=(), db_path=None):
    """Run a SELECT and return a list of rows."""
    conn = get_connection(db_path)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


def run_write(sql, params=(), db_path=None):
    """Run one INSERT/UPDATE/DELETE and commit it."""
    conn = get_connection(db_path)
    conn.execute(sql, params)
    conn.commit()
    conn.close()


def run_write_many(sql, list_of_params, db_path=None):
    """Run the same INSERT/UPDATE for many parameter tuples in one transaction."""
    conn = get_connection(db_path)
    conn.executemany(sql, list_of_params)
    conn.commit()
    conn.close()


if __name__ == "__main__":
    create_tables()
    print(f"Database ready at {get_db_path()}")
    for row in run_query("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        print("  table:", row["name"])
