"""News ingestion: RSS feeds and the optional adapter to the biotech-intel database.

Run:  python -m app.ingest.news
8-K filings arrive via edgar.fetch_recent_filings and are classified in classify.py.
We store headline + short summary only, never full article text.
"""
import logging
import re
import sqlite3
import time
from datetime import datetime

import feedparser

from app.config import load_config, project_path, setup_logging
from app.db import create_tables, get_connection
from app.http_utils import get_response

log = logging.getLogger(__name__)

SNIPPET_LENGTH = 500
# Some news sites return 403 to non-browser clients; the biotech-intel tool uses these headers too.
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}


def clean_snippet(html_text):
    """Strip tags from an RSS summary and cut it to a short snippet."""
    text = re.sub(r"<[^>]+>", " ", html_text or "")
    return re.sub(r"\s+", " ", text).strip()[:SNIPPET_LENGTH]


def entry_date(entry):
    """ISO date string for an RSS entry (today if the feed gave none)."""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return time.strftime("%Y-%m-%d", parsed)
    return datetime.now().strftime("%Y-%m-%d")


def save_news_item(conn, url, title, source, published_at, snippet):
    """Insert one news item; returns 1 if new, 0 if the URL was already stored."""
    cursor = conn.execute(
        "INSERT OR IGNORE INTO news_items (url, title, source, published_at, fetched_at, snippet) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (url, title, source, published_at, datetime.now().isoformat(timespec="seconds"), snippet))
    return cursor.rowcount


def fetch_rss_feeds():
    """Read every RSS feed in config.yaml; returns the number of new items saved."""
    conn = get_connection()
    new_items = 0
    for feed in load_config()["news"]["rss_feeds"]:
        response = get_response(feed["url"], headers=BROWSER_HEADERS)
        if response is None:
            log.error("Feed failed: %s", feed["name"])
            continue
        parsed = feedparser.parse(response.content)
        for entry in parsed.entries:
            if entry.get("link") and entry.get("title"):
                new_items += save_news_item(conn, entry["link"], entry["title"], feed["name"],
                                            entry_date(entry), clean_snippet(entry.get("summary")))
        print(f"{feed['name']}: {len(parsed.entries)} entries")
    conn.commit()
    conn.close()
    return new_items


def pick_column(columns, candidates):
    """Return the first column name from candidates that exists in columns, else None."""
    for name in candidates:
        if name in columns:
            return name
    return None


def import_existing_news():
    """Copy new rows from the biotech-intel database into news_items (read-only).

    The biotech-intel tool has no database yet, so the column mapping is a guess based
    on common names. Check the printed mapping the first time and adjust the lists.
    """
    path = load_config()["news"].get("existing_news_db_path")
    if not path:
        log.info("existing_news_db_path not set; skipping adapter")
        return 0
    source = sqlite3.connect(f"file:{project_path(path)}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    tables = [r["name"] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    conn = get_connection()
    new_items = 0
    for table in tables:
        columns = [r["name"] for r in source.execute(f"PRAGMA table_info({table})")]
        url_col = pick_column(columns, ["url", "link"])
        title_col = pick_column(columns, ["title", "headline"])
        if not url_col or not title_col:
            continue
        source_col = pick_column(columns, ["source", "feed", "feed_name"])
        date_col = pick_column(columns, ["published_at", "published", "date", "pub_date"])
        snippet_col = pick_column(columns, ["snippet", "summary", "description"])
        print(f"Adapter mapping for table {table}: url={url_col} title={title_col} "
              f"source={source_col} date={date_col} snippet={snippet_col}")
        for row in source.execute(f"SELECT * FROM {table}"):
            new_items += save_news_item(
                conn, row[url_col], row[title_col], row[source_col] if source_col else "biotech-intel",
                str(row[date_col])[:10] if date_col else None,
                clean_snippet(row[snippet_col]) if snippet_col else "")
    conn.commit()
    conn.close()
    source.close()
    return new_items


if __name__ == "__main__":
    setup_logging()
    create_tables()
    print(f"New RSS items: {fetch_rss_feeds()}")
    print(f"New items from biotech-intel: {import_existing_news()}")
