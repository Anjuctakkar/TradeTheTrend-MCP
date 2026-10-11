#!/usr/bin/env bash
# Restores a backup made by backup.sh, REPLACING the current database contents (Docker route).
#   ./deploy/restore.sh backups/nse-20261011-021500.sql.gz
set -euo pipefail
cd "$(dirname "$0")/.."
file="${1:?Usage: ./deploy/restore.sh backups/FILE.sql.gz}"
[ -f "$file" ] || { echo "No such file: $file"; exit 1; }
read -r -p "This DELETES the current data and restores $file. Type yes to continue: " answer
[ "$answer" = "yes" ] || { echo "Cancelled."; exit 1; }
docker compose stop app
docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" "$POSTGRES_DB" -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"'
gunzip -c "$file" | docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" "$POSTGRES_DB"'
docker compose start app
echo "Restored from $file"
