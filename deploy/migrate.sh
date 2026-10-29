#!/bin/sh
# Migration remains an explicitly authorized operator action; never call this from CI.
set -eu
umask 077
instance=/docker/gitea/dev/AlphaPulse
exec 9>"$instance/.deploy.lock"
flock -x 9
if [ -e "$instance/deployment.pending.json" ]; then
  echo '存在未完成部署，请先处理部署状态' >&2
  exit 1
fi
if [ -z "${ALPHAPULSE_BACKEND_IMAGE:-}" ]; then
  echo '请通过 ALPHAPULSE_BACKEND_IMAGE 明确指定已验收的候选镜像摘要；迁移须独立授权' >&2
  exit 1
fi
case "$ALPHAPULSE_BACKEND_IMAGE" in
  git.134333.xyz/eezd/dev/backend/alphapulse@sha256:*) ;;
  *) echo '迁移必须使用开发镜像摘要' >&2; exit 1 ;;
esac
if [ -n "$(docker ps -q --filter label=com.docker.compose.project=alphapulse-dev)" ]; then
  echo '迁移前须在维护窗口内停止应用服务；不要并发运行采集或交易' >&2
  exit 1
fi
# Keep migration backups explicit; no periodic backup is introduced.
backup_dir="$(dirname "$0")/../backups"
mkdir -p "$backup_dir"
dump="$backup_dir/alphapulse-premigrate-$(date +%Y%m%d-%H%M%S).dump"
docker exec postgres pg_dump -U alphapulse -Fc alphapulse > "$dump"
echo "已备份：$dump"
docker run --rm --network dev_default --env-file "$instance/.env" "$ALPHAPULSE_BACKEND_IMAGE" alembic upgrade head
