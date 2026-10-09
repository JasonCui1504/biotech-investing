"""Tests for the database helpers, using a temporary SQLite file (no network)."""
import os
import sqlite3
import tempfile

from app.db import ALL_TABLES, create_tables, run_query, run_write, run_write_many


def make_temp_db_path():
    folder = tempfile.mkdtemp()
    return os.path.join(folder, "test.db")


def test_create_tables_makes_every_table():
    path = make_temp_db_path()
    create_tables(path)
    rows = run_query("SELECT name FROM sqlite_master WHERE type='table'", db_path=path)
    names = [row["name"] for row in rows]
    for expected in ["companies", "prices", "events", "recommendations", "claude_cache"]:
        assert expected in names
    assert len(ALL_TABLES) == 15


def test_create_tables_twice_is_safe():
    path = make_temp_db_path()
    create_tables(path)
    create_tables(path)


def test_insert_or_ignore_prevents_duplicates():
    path = make_temp_db_path()
    create_tables(path)
    sql = "INSERT OR IGNORE INTO prices (ticker, date, close) VALUES (?, ?, ?)"
    run_write(sql, ("SMMT", "2026-01-02", 10.0), db_path=path)
    run_write(sql, ("SMMT", "2026-01-02", 99.0), db_path=path)
    rows = run_query("SELECT * FROM prices", db_path=path)
    assert len(rows) == 1
    assert rows[0]["close"] == 10.0


def test_insert_or_replace_updates_row():
    path = make_temp_db_path()
    create_tables(path)
    sql = "INSERT OR REPLACE INTO prices (ticker, date, close) VALUES (?, ?, ?)"
    run_write(sql, ("SMMT", "2026-01-02", 10.0), db_path=path)
    run_write(sql, ("SMMT", "2026-01-02", 11.0), db_path=path)
    rows = run_query("SELECT * FROM prices", db_path=path)
    assert len(rows) == 1
    assert rows[0]["close"] == 11.0


def test_run_write_many_and_row_access_by_name():
    path = make_temp_db_path()
    create_tables(path)
    sql = "INSERT INTO companies (ticker, name) VALUES (?, ?)"
    run_write_many(sql, [("AAA", "Alpha"), ("BBB", "Beta")], db_path=path)
    rows = run_query("SELECT * FROM companies ORDER BY ticker", db_path=path)
    assert [row["name"] for row in rows] == ["Alpha", "Beta"]
    assert rows[0]["in_universe"] == 1  # default value


def test_unique_event_key_blocks_duplicate_events():
    path = make_temp_db_path()
    create_tables(path)
    sql = ("INSERT OR IGNORE INTO events (ticker, event_date, event_type, source_ref) "
           "VALUES (?, ?, ?, ?)")
    run_write(sql, ("SMMT", "2026-01-02", "news", "http://x"), db_path=path)
    run_write(sql, ("SMMT", "2026-01-02", "news", "http://x"), db_path=path)
    assert len(run_query("SELECT * FROM events", db_path=path)) == 1


def test_new_columns_are_added_to_an_old_database():
    path = make_temp_db_path()
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE assets (asset_id INTEGER PRIMARY KEY, ticker TEXT)")  # old shape
    conn.commit()
    conn.close()
    create_tables(path)
    names = [row["name"] for row in run_query("PRAGMA table_info(assets)", db_path=path)]
    assert "peak_sales_claude_suggestion" in names
