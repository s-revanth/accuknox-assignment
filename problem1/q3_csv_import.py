"""Q3. Import users from a CSV file into SQLite.

    python q3_csv_import.py data/users.csv            # -> users.db
    python q3_csv_import.py data/users.csv --db x.db

Rules (assumptions, documented in README):
  * header row required, columns `name` and `email` (case/whitespace-insensitive, extra columns ignored)
  * email is the identity: stored lower-cased, UNIQUE; a repeat email is skipped, not overwritten
  * rows with an empty name or an invalid email are rejected and reported with their line number
  * the whole file is one transaction: either every valid row lands or none do
"""
import argparse
import csv
import re
import sqlite3
import sys

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL CHECK (length(trim(name)) > 0),
    email      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def read_rows(path):
    """Yield (line_no, name, email) and collect rejects. utf-8-sig strips an Excel BOM."""
    rejects = []
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError("empty file")
        cols = {c.strip().lower(): c for c in reader.fieldnames}
        missing = {"name", "email"} - cols.keys()
        if missing:
            raise ValueError(f"missing column(s): {', '.join(sorted(missing))}")
        for row in reader:
            line = reader.line_num
            name = (row.get(cols["name"]) or "").strip()
            email = (row.get(cols["email"]) or "").strip().lower()
            if not name:
                rejects.append((line, "empty name"))
            elif not EMAIL.match(email):
                rejects.append((line, f"invalid email {email!r}"))
            else:
                rows.append((line, name, email))
    return rows, rejects


def import_users(conn, rows):
    conn.executescript(SCHEMA)
    inserted, dupes = 0, []
    with conn:  # single transaction; rolls back on any unexpected error
        for line, name, email in rows:
            cur = conn.execute(
                "INSERT INTO users (name, email) VALUES (?, ?) ON CONFLICT (email) DO NOTHING",
                (name, email))
            if cur.rowcount:
                inserted += 1
            else:
                dupes.append((line, f"duplicate email {email}"))
    return inserted, dupes


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("csv")
    p.add_argument("--db", default="users.db")
    a = p.parse_args(argv)

    try:
        rows, rejects = read_rows(a.csv)
    except (OSError, ValueError) as e:
        sys.exit(f"cannot read {a.csv}: {e}")
    conn = sqlite3.connect(a.db)
    inserted, dupes = import_users(conn, rows)
    total = conn.execute("SELECT count(*) FROM users").fetchone()[0]
    conn.close()

    print(f"inserted {inserted}, skipped {len(dupes)} duplicate(s), rejected {len(rejects)} invalid row(s)")
    for line, why in sorted(rejects + dupes):
        print(f"  line {line}: {why}")
    print(f"users table now has {total} rows")
    return inserted, dupes, rejects


if __name__ == "__main__":
    main()
