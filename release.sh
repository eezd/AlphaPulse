#!/usr/bin/env bash
# 远端发布客户端：不读取本地 Git 状态，不推送或切换分支。
set -euo pipefail
if (( $# != 1 )) || [[ -z $1 ]]; then
    printf '用法：bash %s <远端分支>\n' "$0" >&2
    exit 2
fi
command -v python3 >/dev/null || { printf '缺少命令：python3\n' >&2; exit 1; }
exec python3 - "$1" <<'PY'
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

# 初始化时渲染；凭据不得写在这里。
API_URL = 'https://git.134333.xyz/api/v1'  # 例如 https://git.example.com/api/v1
OWNER = 'eezd'
REPO = 'AlphaPulse'
WORKFLOW = 'release.yml'  # 例如 release.yml
GITHUB_OWNER = 'eezd'
GITHUB_REPO = 'AlphaPulse'
WAIT_SECONDS = 7200
POLL_SECONDS = 5


def fail(message):
    raise RuntimeError(message)


def quote(value):
    return urllib.parse.quote(value, safe='')


# 禁止认证请求自动跳转至其他地址。
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


opener = urllib.request.build_opener(NoRedirect)


def request(url, token, payload=None):
    headers = {'Authorization': 'token ' + token, 'Accept': 'application/json', 'User-Agent': 'AlphaPulse-Release'}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with opener.open(req, timeout=30) as response:
            body = response.read()
        return json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        # 不打印可能含凭据或隐私的响应体。
        fail(f'远端请求失败：HTTP {exc.code}；未自动重试发布请求')
    except (urllib.error.URLError, TimeoutError, OSError):
        fail('远端请求失败：网络或超时错误；请核查已有请求，未自动重试')


def main():
    if any('@@' in value for value in
           (API_URL, OWNER, REPO, WORKFLOW, GITHUB_OWNER, GITHUB_REPO)):
        fail('发布模板尚未配置仓库和工作流')
    parsed = urllib.parse.urlsplit(API_URL)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password:
        fail('Gitea API 地址必须为不含凭据的 HTTPS 地址')
    token = os.environ.get('GITEA_TOKEN')
    if not token:
        fail('缺少 GITEA_TOKEN；请通过现有凭据机制或环境变量提供')
    branch = sys.argv[1]
    if (branch.startswith(('-', '/')) or branch.endswith('/') or
            any(ord(c) < 32 for c in branch)):
        fail('远端分支名无效')
    base = API_URL.rstrip('/') + '/repos/' + quote(OWNER) + '/' + quote(REPO)
    branch_info = request(base + '/branches/' + quote(branch), token)
    sha = branch_info['commit']['id']
    if not re.fullmatch(r'[0-9a-f]{40}', sha):
        fail('远端未返回有效的完整提交 SHA')
    commit = request(base + '/git/commits/' + sha, token)
    date = datetime.datetime.fromisoformat(commit['commit']['committer']['date'].replace('Z', '+00:00'))
    if date.tzinfo is None:
        fail('远端提交时间缺少时区')
    version = date.astimezone(datetime.timezone.utc).strftime('%Y%m%d') + '-' + sha[:8]
    request_id = str(uuid.uuid4())
    print(f'来源分支：{branch}\n完整 SHA：{sha}\n版本：{version}\n请求 ID：{request_id}', flush=True)
    details = request(base + '/actions/workflows/' + quote(WORKFLOW) +
                      '/dispatches?return_run_details=true', token,
                      {'ref': 'refs/heads/main', 'inputs': {
                          'source_branch': branch, 'source_sha': sha, 'request_id': request_id}})
    if not isinstance(details, dict) or not isinstance(details.get('workflow_run_id'), int):
        fail('未取得唯一 run ID；任务可能已启动，请按请求 ID 核查，不要直接重复触发')
    run_id = details['workflow_run_id']
    run_api = base + '/actions/runs/' + str(run_id)
    web_base = API_URL.rstrip('/').removesuffix('/api/v1')
    run_url = web_base + '/' + quote(OWNER) + '/' + quote(REPO) + '/actions/runs/' + str(run_id)
    print('工作流：' + run_url, flush=True)
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        run = request(run_api, token)
        status = run.get('conclusion') or run.get('status')
        if status == 'success':
            break
        if status in ('failure', 'cancelled', 'canceled', 'skipped', 'timed_out', 'action_required'):
            fail(f'发布未成功：{status}；查看 {run_url}')
        if status not in ('queued', 'waiting', 'pending', 'running', 'in_progress', 'completed'):
            fail(f'工作流返回无法识别的状态，请查看 {run_url}')
        if time.monotonic() >= deadline:
            fail('等待超时，未取消远端发布；请查询已有任务：' + run_url)
        time.sleep(POLL_SECONDS)
    release = request(base + '/releases/tags/' + quote(version), token)
    if (release.get('tag_name') != version or release.get('draft') is not False or
            release.get('prerelease') is not False):
        fail('工作流结束，但未找到匹配的正式 Release')
    assets = [a for a in release.get('assets', []) if a.get('name') == 'build.json']
    if len(assets) != 1:
        fail('正式 Release 缺少唯一 build.json')
    metadata_url = urllib.parse.urljoin(API_URL, assets[0]['browser_download_url'])
    target = urllib.parse.urlsplit(metadata_url)
    if (target.scheme, target.netloc) != (parsed.scheme, parsed.netloc):
        fail('build.json 下载地址不属于已配置 Gitea；拒绝向其他地址发送凭据')
    metadata = request(metadata_url, token)
    if metadata.get('source_sha') != sha or metadata.get('version') != version:
        fail('发布元数据的版本或 SHA 不匹配')
    # 双端包哈希和 OCI 摘要验证由可信工作流完成，缺任一项不得成功结束。
    print('Gitea Release：' + web_base + '/' + quote(OWNER) + '/' + quote(REPO) + '/releases/tag/' + version)
    print('GitHub Release：https://github.com/' + quote(GITHUB_OWNER) + '/' + quote(GITHUB_REPO) + '/releases/tag/' + version)
    print('发布成功；本地 systemd 服务未更新')


try:
    main()
except KeyboardInterrupt:
    print('已停止本地等待；远端任务未取消，请查看已输出的工作流或请求 ID', file=sys.stderr)
    sys.exit(130)
except (RuntimeError, KeyError, TypeError, ValueError) as exc:
    print('发布失败：' + str(exc), file=sys.stderr)
    sys.exit(1)
PY
