#!/usr/bin/env bash
set -euo pipefail
services=(alphapulse-api.service alphapulse-crawler.service alphapulse-market.service alphapulse-calendar.service alphapulse-news-risk.service alphapulse-jin10-calendar.service)
if [[ ${1-} == --systemd ]] && (( $# == 2 )); then
    for service in "${services[@]}"; do
        # systemd sends the termination signal and waits within TimeoutStopSec.
        [[ $service != "alphapulse-$2.service" ]] || exit 0
    done
    printf '未知的 AlphaPulse 服务：%s\n' "$2" >&2
    exit 2
fi
(( $# == 0 )) || { printf '用法：%s [--systemd <服务>]\n' "$0" >&2; exit 2; }
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
cd -- "$project_dir"
command -v flock >/dev/null || { printf '缺少命令：flock\n' >&2; exit 1; }
command -v systemctl >/dev/null || { printf '缺少命令：systemctl\n' >&2; exit 1; }
[[ -d runtime ]] || { printf '项目尚未部署，缺少 runtime 目录\n' >&2; exit 1; }
exec 9>>runtime/.deploy.lock
flock -n 9 || { printf '项目正在构建或启停，请稍后重试\n' >&2; exit 1; }
systemctl stop -- "${services[@]}"
printf '已停止项目：%s\n' "${project_dir##*/}"
