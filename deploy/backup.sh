#!/bin/sh
# 每日备份 alphapulse 库，保留最近 14 份；backrest 03:00 会把 /srv 整体备份到异地
set -eu
dir=/srv/AlphaPulse/backups/daily
mkdir -p "$dir"
target="$dir/alphapulse-$(date +%Y%m%d).dump"
# 先写临时文件，成功后再改名，避免留下半截备份
docker exec postgres pg_dump -U alphapulse -Fc alphapulse > "$target.tmp"
mv "$target.tmp" "$target"
ls -1t "$dir"/alphapulse-*.dump | tail -n +15 | xargs -r rm --
echo "$(date '+%F %T') 备份完成：$target $(du -h "$target" | cut -f1)"
