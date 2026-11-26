# Problem Statement 1 – Q1 to Q3

Python 3.10+; dependencies: `requests`, `matplotlib` (everything else is the standard library, including `sqlite3`).

```bash
python -m venv .venv && .venv/bin/pip install requests matplotlib
cd problem1
python mock_api.py &                     # local REST API on :8765 (/books, /students)
python q1_books.py                       # Open Library -> books.db -> table
python q1_books.py --url http://127.0.0.1:8765/books
python q2_scores.py                      # /students -> averages -> scores.png
python q3_csv_import.py data/users.csv   # CSV -> users.db
python -m unittest test_problem1 -v      # 7 tests, offline
```

| File | What it does |
|---|---|
| `q1_books.py` | GET with timeout + retry/backoff on 5xx and network errors, normalise the payload, drop records without title/author, upsert into `books` (UNIQUE(title, author)) in one transaction, print a table. |
| `q2_scores.py` | GET students, per-student average, class average, per-subject average, bar chart with the class average as a reference line. |
| `q3_csv_import.py` | Validate header and each row, reject bad rows with their line number, insert in one transaction, skip repeat emails, print a report. |
| `mock_api.py` | Stdlib HTTP server serving `data/api_fixture.json`, so the scripts and tests need no internet. |
| `test_problem1.py` | Starts the mock API on a free port and tests all three scripts against temp databases. |

## Assumptions

**Q1 – books API**
- The problem names no API, so the default is the public [Open Library search API](https://openlibrary.org/dev/docs/api/search) (no key). Its fields (`author_name[]`, `first_publish_year`) are mapped to `author` (first listed author) and `publication_year`. `--url` accepts any API returning `[{title, author, publication_year}]`.
- A book is identified by (title, author). Re-running the script updates the year instead of inserting a duplicate.
- A record with no title or no author is dropped and counted. A missing or non-numeric year is stored as `NULL`, not 0.
- Network errors and 5xx are retried 3 times with 1 s / 2 s backoff. A 4xx fails at once, since retrying will not help.

**Q2 – student scores**
- No public scores API exists, so the data comes from the local mock. Shape: `[{id, name, scores: {subject: score | null}}]`, scores out of 100.
- `null` means the student did not sit that test. It is left out of the average, not counted as 0 (Farah: 92.5, not 61.7).
- "The average score" is reported three ways: per student (the bars), the class average over all individual scores (the dashed line), and per subject (printed).
- The chart is written to a PNG so it works headless (`matplotlib` Agg backend).

**Q3 – CSV import**
- The file has a header row with `name` and `email` columns (case and spacing ignored, extra columns like `City` ignored). An Excel BOM is handled.
- Email is the identity: lower-cased and `UNIQUE COLLATE NOCASE`. A repeat email is skipped and reported, never silently overwritten.
- Invalid rows (empty name, malformed email) are rejected with their CSV line number. The valid rows are still imported.
- The import is one transaction, so a crash halfway leaves the table as it was.
- Email validation is a pragmatic regex (`x@y.z`), not full RFC 5322.

## Sample output

```
$ python q3_csv_import.py data/users.csv
inserted 5, skipped 1 duplicate(s), rejected 2 invalid row(s)
  line 5: empty name
  line 6: invalid email 'not-an-email'
  line 8: duplicate email asha.rao@example.com
users table now has 5 rows
```
