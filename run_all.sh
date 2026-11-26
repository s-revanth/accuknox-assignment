#!/usr/bin/env bash
# One command for a fresh machine: make a virtualenv, install the three dependencies,
# run every test suite, then every script with its real output. Needs Python 3.10+ (macOS or Linux).
#   ./run_all.sh            tests + demos (about 1 minute)
#   ./run_all.sh --tests    tests only
set -euo pipefail
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
if [ ! -x .venv/bin/python ]; then
  "$PY" -m venv .venv
fi
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt
P="$PWD/.venv/bin/python"
export MPLBACKEND=Agg

echo "== Problem 1: tests";   (cd problem1     && "$P" -m unittest test_problem1)
echo "== Q4 tinyvec: tests";  (cd q4_tinyvec   && "$P" -m unittest test_tinyvec)
echo "== Q5 forensics: tests";(cd q5_forensics && "$P" -m unittest test_forensics)
[ "${1:-}" = "--tests" ] && exit 0

echo "== Problem 1: scripts against the bundled mock API"
cd problem1
"$P" mock_api.py >/dev/null 2>&1 & MOCK=$!
trap 'kill $MOCK 2>/dev/null || true' EXIT
sleep 1
"$P" q1_books.py --url http://127.0.0.1:8765/books --db books.db
"$P" q2_scores.py
"$P" q3_csv_import.py data/users.csv --db users.db
cd ..
echo "== Q4 tinyvec: demo";       (cd q4_tinyvec   && "$P" demo.py)
echo "== Q5 forensics: simulate";  (cd q5_forensics && "$P" simulate.py)
echo "All done. Live Open Library fetch: (cd problem1 && ../.venv/bin/python q1_books.py)"
