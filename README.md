# AccuKnox assignment

All answers are in **[SUBMISSION.md](./SUBMISSION.md)**, with each assumption written down next to the answer it affects.

| Question | Where |
|---|---|
| Problem 1, Q1–Q3: API → SQLite, scores → chart, CSV → SQLite | [`problem1/`](./problem1) |
| Q4: most complex Python code | [`q4_tinyvec/`](./q4_tinyvec): a vector database from scratch (HNSW, filtered search with a query planner, WAL crash recovery, int8 quantization) |
| Q5: most complex database code | [`q5_forensics/`](./q5_forensics): runtime-security forensics on SQLite (recursive CTEs, window functions, FTS5, triggers, partial indexes) |
| Assignment 2: self-rating, LLM chatbot architecture, vector databases | [SUBMISSION.md › Assignment 2](./SUBMISSION.md#assignment-2) |
| Assignment 3: the project I'm proudest of | [CARREL.md](./CARREL.md) |

## Run it

You need Python 3.10 or newer. The scripts and tests are offline: a small bundled mock API stands in for the web APIs.

**macOS / Linux**, one command:

```bash
./run_all.sh            # makes .venv, installs requirements, runs 29 tests and every script (about 1 minute)
./run_all.sh --tests    # tests only
```

**Windows, or by hand:**

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip install -r requirements.txt
cd problem1     && python -m unittest test_problem1 -v && cd ..
cd q4_tinyvec   && python -m unittest test_tinyvec -v  && cd ..
cd q5_forensics && python -m unittest test_forensics -v && cd ..
```

Then each part's README covers its scripts: [problem1](./problem1/README.md) · [q4_tinyvec](./q4_tinyvec/README.md) · [q5_forensics](./q5_forensics/README.md).

The [tests workflow](./.github/workflows/tests.yml) runs every suite on clean Ubuntu, macOS and Windows machines with Python 3.10 and 3.13.
