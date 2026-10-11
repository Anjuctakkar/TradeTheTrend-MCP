#!/usr/bin/env bash
# Runs every test file. Each one builds its own temporary database unless DATABASE_URL is set.
# To test against Postgres, set DATABASE_URL to an EMPTY throwaway database (the tests expect empty tables).
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=.
for t in tests/test_*.py; do
  echo "== $t"
  python -W ignore "$t"
done
echo "All tests passed."
