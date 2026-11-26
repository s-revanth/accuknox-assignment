"""Q1. Fetch books from a REST API, store them in SQLite, display them.

Default source is the public Open Library search API (no key needed).
Use --url to point at any API that returns a JSON list of
{title, author, publication_year}, e.g. the local mock:  --url http://127.0.0.1:8765/books

    python q1_books.py                         # Open Library, query "distributed systems"
    python q1_books.py --query "python"        # different search
    python q1_books.py --url http://127.0.0.1:8765/books
"""
import argparse
import sqlite3
import time

import requests

OPEN_LIBRARY = "https://openlibrary.org/search.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    title            TEXT    NOT NULL,
    author           TEXT    NOT NULL,
    publication_year INTEGER,                 -- NULL when the API does not know it
    fetched_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (title, author)                    -- re-running the script must not duplicate rows
);
"""


def get_json(url, params=None, retries=3, timeout=10):
    """GET with a timeout and retry with backoff on network errors / 5xx. 4xx fails immediately."""
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            if resp.status_code < 500:
                resp.raise_for_status()
                return resp.json()
            err = f"HTTP {resp.status_code}"
        except (requests.ConnectionError, requests.Timeout) as e:
            err = type(e).__name__
        if attempt < retries:
            time.sleep(2 ** (attempt - 1))
    raise RuntimeError(f"GET {url} failed after {retries} attempts ({err})")


def fetch_open_library(query, limit):
    data = get_json(OPEN_LIBRARY, {"q": query, "limit": limit,
                                   "fields": "title,author_name,first_publish_year"})
    # Open Library's shape differs from ours: normalise it here so the rest of the code doesn't care.
    return [{"title": d.get("title"),
             "author": (d.get("author_name") or [None])[0],
             "publication_year": d.get("first_publish_year")} for d in data.get("docs", [])]


def clean(raw):
    """Drop records without title/author, coerce the year to int or None."""
    good, dropped = [], 0
    for b in raw:
        title, author = (b.get("title") or "").strip(), (b.get("author") or "").strip()
        if not title or not author:
            dropped += 1
            continue
        year = b.get("publication_year")
        try:
            year = int(year) if year is not None else None
        except (TypeError, ValueError):
            year = None
        good.append((title, author, year))
    return good, dropped


def store(conn, books):
    """Upsert: a re-fetch refreshes the year instead of creating duplicates."""
    conn.executescript(SCHEMA)
    count = lambda: conn.execute("SELECT count(*) FROM books").fetchone()[0]
    before = count()
    with conn:  # one transaction for the whole batch
        conn.executemany(
            """INSERT INTO books (title, author, publication_year) VALUES (?, ?, ?)
               ON CONFLICT (title, author) DO UPDATE SET
                   publication_year = excluded.publication_year,
                   fetched_at       = datetime('now')""",
            books)
    return count() - before  # new rows; the rest were already stored (or repeated in the payload)


def display(conn):
    rows = conn.execute(
        "SELECT title, author, publication_year FROM books "
        "ORDER BY publication_year IS NULL, publication_year, title").fetchall()
    tw = min(max((len(r[0]) for r in rows), default=5), 50)
    aw = min(max((len(r[1]) for r in rows), default=6), 25)
    print(f"{'Title':<{tw}}  {'Author':<{aw}}  Year")
    print(f"{'-' * tw}  {'-' * aw}  ----")
    for t, a, y in rows:
        print(f"{t[:tw]:<{tw}}  {a[:aw]:<{aw}}  {y if y is not None else '?'}")
    print(f"\n{len(rows)} books in the database")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", help="API returning a JSON list of {title, author, publication_year}")
    p.add_argument("--query", default="distributed systems", help="Open Library search query")
    p.add_argument("--limit", type=int, default=15)
    p.add_argument("--db", default="books.db")
    a = p.parse_args(argv)

    raw = get_json(a.url) if a.url else fetch_open_library(a.query, a.limit)
    books, dropped = clean(raw)
    conn = sqlite3.connect(a.db)
    new = store(conn, books)
    print(f"fetched {len(raw)} records: {new} new, {len(books) - new} already stored or repeated, "
          f"{dropped} dropped as incomplete\n")
    display(conn)
    conn.close()


if __name__ == "__main__":
    main()
