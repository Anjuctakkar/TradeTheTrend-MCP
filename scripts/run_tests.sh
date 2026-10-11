#!/usr/bin/env bash
# Runs every test file. Each Python test builds its own temporary database unless DATABASE_URL is set.
# To test against Postgres, set DATABASE_URL to an EMPTY throwaway database (the tests expect empty tables).
# The JavaScript tests need Node; test_page.js also needs jsdom (npm install jsdom) and is skipped without it.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=.
for t in tests/test_*.py; do
  echo "== $t"
  python -W ignore "$t"
done
if command -v node >/dev/null 2>&1; then
  for t in tests/js/test_*.js; do
    echo "== $t"
    node "$t"
  done
else
  echo "(Node not found: JavaScript tests skipped)"
fi
echo "All tests passed."
