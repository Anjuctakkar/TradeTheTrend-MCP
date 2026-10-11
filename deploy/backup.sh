#!/usr/bin/env bash
# Backs up the database (Docker route). Run from anywhere:  ./deploy/backup.sh
# Keeps 14 days of files in ./backups. Schedule it:  crontab -e  ->  15 2 * * * /path/to/nse-dashboard/deploy/backup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p backups
file="backups/nse-$(date +%Y%m%d-%H%M%S).sql.gz"
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' | gzip > "$file"
find backups -name 'nse-*.sql.gz' -mtime +14 -delete
echo "Wrote $file ($(du -h "$file" | cut -f1))"
