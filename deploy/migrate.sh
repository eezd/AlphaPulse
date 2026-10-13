#!/bin/sh
# 迁移前先备份：只有一套环境，迁移即生产操作
set -eu
cd "$(dirname "$0")"
mkdir -p ../backups
dump="../backups/alphapulse-premigrate-$(date +%Y%m%d-%H%M%S).dump"
docker exec postgres pg_dump -U alphapulse -Fc alphapulse > "$dump"
echo "已备份：$dump"
docker compose build api
docker compose run --rm -T api alembic upgrade head
