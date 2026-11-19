#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)

if [[ ${1-} == --systemd ]] && (( $# == 2 )); then
    cd -- "$project_dir/runtime/current/backend"
    case $2 in
        api) exec .venv/bin/python -m uvicorn alphapulse.api.main:app --host 127.0.0.1 --port 21005 --proxy-headers --forwarded-allow-ips 127.0.0.1 ;;
        crawler) module=alphapulse.crawler.manager ;;
        market) module=alphapulse.market.worker ;;
        calendar) module=alphapulse.calendar.worker ;;
        news-risk) module=alphapulse.briefings.worker ;;
        jin10-calendar) module=alphapulse.calendar.jin10_worker ;;
        *) printf '未知的 AlphaPulse 服务：%s\n' "$2" >&2; exit 2 ;;
    esac
    exec .venv/bin/python -m "$module"
fi

(( $# == 0 )) || { printf '用法：%s [--systemd <服务>]\n' "$0" >&2; exit 2; }
exec python3 "$project_dir/scripts/native.py"
