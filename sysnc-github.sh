#!/usr/bin/env bash
# 仅在操作者显式运行时，从 Forgejo 向 GitHub 同步。
set -euo pipefail
(( $# == 0 )) || { printf '用法：bash %s\n' "$0" >&2; exit 2; }
command -v python3 >/dev/null || { printf '缺少命令：python3\n' >&2; exit 1; }
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
exec python3 - "$project_dir" <<'PY'
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

FORGEJO_API = 'https://git.134333.xyz/api/v1'
FORGEJO_OWNER = 'eezd'
FORGEJO_REPO = 'AlphaPulse'
FORGEJO_GIT = 'https://git.134333.xyz/eezd/AlphaPulse.git'
FORGEJO_IMAGE = 'git.134333.xyz/eezd/alphapulse'  # 仓库路径，无 docker://、版本或摘要
GITHUB_OWNER = 'eezd'
GITHUB_REPO = 'AlphaPulse'
GITHUB_GIT = 'git@github.com:eezd/AlphaPulse.git'
GITHUB_PRIVATE = 'false'  # true / false
BRANCHES_JSON = r'''["main", "dev"]'''
BRANCHES = None  # 在 main 中解析已确认的分支范围
USE_LFS = 'false'  # true / false，接入时检查整个纳入同步的历史
FORGEJO_DOWNLOAD_HOSTS = ()  # 如有对象存储，填入已核实的 HTTPS authority；不发送令牌
GITHUB_DOWNLOAD_HOSTS = ('release-assets.githubusercontent.com', 'objects.githubusercontent.com')

VERSION = re.compile(r'[0-9]{8}-[0-9a-f]{8}')
SHA = re.compile(r'[0-9a-f]{40}')
HASH = re.compile(r'[0-9a-f]{64}')
PLATFORMS = ('linux/amd64', 'linux/arm64')
BUILD = 'build.json'
COMPLETE = 'release-complete.json'
SUMS = 'SHA256SUMS'


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def encode(value):
    return urllib.parse.quote(value, safe='')


def authority(url):
    parsed = urllib.parse.urlsplit(url)
    require(parsed.scheme == 'https' and parsed.hostname and parsed.username is None
            and parsed.password is None, 'API 或下载地址必须是无凭据的 HTTPS 地址')
    return parsed.netloc


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class API:
    def __init__(self, root, authorization, download_hosts=()):
        self.root = root
        self.host = authority(root)
        self.auth = authorization
        self.download_hosts = download_hosts
        self.opener = urllib.request.build_opener(NoRedirect)

    def open(self, url, phase, *, method='GET', data=None, headers=None, download=False):
        headers = {'Authorization': self.auth, 'User-Agent': 'git-init-manual-sync',
                   'Accept': 'application/json', **(headers or {})}
        require(authority(url) == self.host or (method == 'POST' and
                self.host == 'api.github.com' and authority(url) == 'uploads.github.com'),
                phase + '：拒绝向非预期站点发送凭据')
        for _ in range(4):
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                return self.opener.open(request, timeout=60)
            except urllib.error.HTTPError as err:
                code, location = err.code, err.headers.get('Location')
                err.close()
                if download and code in (301, 302, 303, 307, 308) and location:
                    url = urllib.parse.urljoin(url, location)
                    require(authority(url) in (self.host, *self.download_hosts),
                            phase + '：拒绝未批准的下载跳转')
                    headers.pop('Authorization', None)
                    continue
                raise RuntimeError(f'{phase}失败：HTTP {code}；未自动重试') from None
            except (OSError, urllib.error.URLError):
                raise RuntimeError(f'{phase}失败：网络或超时错误；未自动重试') from None
        raise RuntimeError(phase + '：下载跳转过多')

    def json(self, path, phase, *, method='GET', payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {'Content-Type': 'application/json'} if data is not None else None
        with self.open(self.root + path, phase, method=method, data=data, headers=headers) as response:
            return json.load(response)

    def pages(self, path, phase, *, size_key):
        items = []
        ids = set()
        expected = None
        page = 1
        while True:
            url = f'{self.root}{path}?{size_key}=50&page={page}'
            with self.open(url, phase) as response:
                batch = json.load(response)
                link = response.headers.get('Link', '')
                total = response.headers.get('X-Total-Count')
            if total is not None:
                require(total.isdecimal() and (expected is None or expected == int(total)),
                        phase + '：分页总数变化')
                expected = int(total)
            require(isinstance(batch, list), phase + '：分页结果不是列表')
            if not batch:
                require('rel="next"' not in link and (expected is None or len(items) == expected),
                        phase + '：分页结果不完整')
                return items
            for item in batch:
                entry_id = item['id']
                require(type(entry_id) is int and entry_id > 0 and entry_id not in ids,
                        phase + '：分页 ID 无效或重复')
                ids.add(entry_id)
                items.append(item)
            if expected is not None:
                require(len(items) <= expected and (len(items) < expected or 'rel="next"' not in link),
                        phase + '：分页结果与总数冲突')
                if len(items) == expected:
                    return items
            page += 1

    def forgejo_asset(self, asset, path):
        url = urllib.parse.urljoin(self.root + '/', asset['browser_download_url'])
        return self.download(url, path, '下载 Forgejo 附件')

    def github_asset(self, asset, path):
        return self.download(f'{self.root}/releases/assets/{asset["id"]}', path, '下载 GitHub 附件')

    def download(self, url, path, phase):
        digest = hashlib.sha256()
        with self.open(url, phase, headers={'Accept': 'application/octet-stream'}, download=True) as response:
            if path is None:
                for block in iter(lambda: response.read(1024 * 1024), b''):
                    digest.update(block)
            else:
                with path.open('wb') as output:
                    for block in iter(lambda: response.read(1024 * 1024), b''):
                        digest.update(block)
                        output.write(block)
        return digest.hexdigest()

    def upload(self, release, name, path):
        url = release['upload_url'].removesuffix('{?name,label}')
        route = f'https://uploads.github.com{urllib.parse.urlsplit(self.root).path}/releases/{release["id"]}/assets'
        require(url.lower() == route.lower(), 'GitHub 附件上传地址不属于目标 Release')
        headers = {'Content-Type': 'application/octet-stream', 'Content-Length': str(path.stat().st_size)}
        with path.open('rb') as content, self.open(url + '?name=' + encode(name), '上传 GitHub 附件',
                                                    method='POST', data=content, headers=headers) as response:
            asset = json.load(response)
        require(asset.get('name') == name and asset.get('state') == 'uploaded' and
                type(asset.get('id')) is int and asset['id'] > 0, 'GitHub 附件上传响应不完整')
        return asset


def index(items, key, phase):
    result = {}
    for item in items:
        value = item[key]
        require(isinstance(value, str) and value and value not in result, phase + '：名称无效或重复')
        result[value] = item
    return result


def run(args, phase, *, cwd=None, allowed=(0,)):
    environment = {name: value for name, value in os.environ.items() if name not in
                   ('FORGEJO_TOKEN', 'GITHUB_TOKEN', 'GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR',
                    'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_NAMESPACE')}
    environment.update(GIT_TERMINAL_PROMPT='0', LC_ALL='C')
    result = subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
    require(result.returncode in allowed, f'{phase}失败（退出码 {result.returncode}）')
    return result


class Snapshot:
    def __init__(self, directory):
        self.directory = directory
        self.git('init', '--bare', str(directory), phase='创建临时仓库')
        self.git('remote', 'add', 'source', FORGEJO_GIT)
        self.git('remote', 'add', 'destination', GITHUB_GIT)
        self.git('fetch', '--no-tags', 'source', 'refs/heads/*:refs/heads/*', 'refs/tags/*:refs/tags/*',
                 phase='读取 Forgejo 引用')
        self.refs = self.parse(self.git('for-each-ref', '--format=%(objectname)%09%(refname)',
                                       'refs/heads', 'refs/tags').stdout)
        self.heads = {r: sha for r, sha in self.refs.items() if r.startswith('refs/heads/') and
                      (BRANCHES == ['*'] or r.removeprefix('refs/heads/') in BRANCHES)}
        require('refs/heads/main' in self.heads, '同步范围或 Forgejo 快照缺少 main')

    def git(self, *args, phase='读取 Git 快照', allowed=(0,)):
        # 不执行宿主全局钩子；所有 GitHub 写入均在显式同步阶段执行。
        cwd = self.directory if self.directory.exists() else self.directory.parent
        return run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'push.followTags=false',
                    '-c', 'push.pushOption=', *args], phase, cwd=cwd, allowed=allowed)

    @staticmethod
    def parse(output):
        refs = {}
        for line in output.decode().splitlines():
            sha, ref = line.split('\t', 1)
            require(SHA.fullmatch(sha) and ref.startswith(('refs/heads/', 'refs/tags/')) and
                    ref not in refs, 'Git 返回无效或重复的引用')
            refs[ref] = sha
        return refs

    def target(self):
        return self.parse(self.git('ls-remote', '--refs', '--heads', '--tags', 'destination',
                                   phase='读取 GitHub 引用').stdout)

    def commit(self, ref):
        result = self.git('rev-parse', '--verify', '--quiet', ref + '^{commit}', allowed=(0, 1))
        return result.stdout.decode().strip() if result.returncode == 0 else None

    def ancestor(self, older, newer):
        result = self.git('cat-file', '-t', older, allowed=(0, 128))
        if result.stdout != b'commit\n':
            return False
        return self.git('merge-base', '--is-ancestor', older, newer, allowed=(0, 1)).returncode == 0

    def in_scope(self, ref):
        if BRANCHES == ['*']:
            return True
        commit = self.commit(ref)
        return commit is not None and any(self.ancestor(commit, tip) for tip in self.heads.values())

    def changes(self, refs, target):
        changes = []
        for ref, sha in sorted(refs.items()):
            old = target.get(ref)
            if old == sha:
                continue
            require(old is None or (ref.startswith('refs/heads/') and self.ancestor(old, sha)),
                    f'GitHub 分支分叉或 TAG 冲突：{ref}')
            changes.append(f'{ref}:{ref}')
        return changes

    def lfs(self, refs):
        require(shutil.which('git-lfs'), '缺少命令：git-lfs')
        self.git('lfs', 'fetch', '--all', 'source', *refs, phase='下载 Forgejo LFS 对象')
        self.git('lfs', 'push', '--all', 'destination', *refs, phase='补齐 GitHub LFS 对象')


def read_manifest(ref, authfile, *, allow_missing=False):
    # Skopeo 1.18 在目标清单缺失时也可能返回 2，仍须按 Registry 错误分类。
    result = run(['skopeo', 'inspect', '--raw', '--authfile', authfile, 'docker://' + ref],
                 '读取 OCI 清单', allowed=(0, 1, 2))
    if result.returncode != 0:
        # 首次复制时版本清单或整个包尚不存在；认证、限流等错误不视为缺失。
        if allow_missing and any(code in result.stderr.lower() for code in (b'manifest unknown', b'name unknown')):
            return None
        raise RuntimeError('读取 OCI 清单失败；请核对镜像、权限和 Registry 状态')
    return result.stdout


def verify_image(ref, image, authfile, *, allow_missing=False):
    raw = read_manifest(ref, authfile, allow_missing=allow_missing)
    if raw is None:
        return False
    digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
    require(digest == image['digest'], 'OCI 顶层摘要冲突')
    manifests = json.loads(raw)['manifests']
    for platform, expected in image['platforms'].items():
        system, arch = platform.split('/')
        matches = [m['digest'] for m in manifests if m.get('platform', {}).get('os') == system
                   and m.get('platform', {}).get('architecture') == arch]
        require(matches == [expected], 'OCI 架构摘要冲突：' + platform)
        repository = ref.rsplit('@', 1)[0] if '@' in ref else ref.rsplit(':', 1)[0]
        child = read_manifest(repository + '@' + expected, authfile)
        require('sha256:' + hashlib.sha256(child).hexdigest() == expected, 'OCI 子清单摘要冲突：' + platform)
    return True


def package_names(tag):
    return {f'{FORGEJO_REPO}_{tag}_{system}_{arch}.{extension}'
            for system, extension in (('linux', 'tar.gz'), ('darwin', 'tar.gz'), ('windows', 'zip'))
            for arch in ('amd64', 'arm64')}


def is_formal(release):
    return release['draft'] is False and release['prerelease'] is False


def auth_file():
    require(shutil.which('skopeo'), '缺少命令：skopeo')
    value = os.environ.get('REGISTRY_AUTH_FILE')
    require(value and Path(value).is_file(), '请显式提供可读的 REGISTRY_AUTH_FILE')
    return str(Path(value).resolve())


class Version:
    def __init__(self, tag, release, directory, source, target, snapshot, target_release):
        self.tag = tag
        self.path = directory / tag
        self.path.mkdir()
        self.target = target_release
        self.target_path = None
        self.title = release['name']
        self.body = release['body']
        require(isinstance(self.title, str) and isinstance(self.body, str), 'Forgejo Release 标题或正文无效')
        assets = index(release['assets'], 'name', 'Forgejo 附件')
        require({BUILD, COMPLETE, SUMS} <= assets.keys(), '正式版本缺少发布元数据或验收证明')
        build_hash = source.forgejo_asset(assets[BUILD], self.path / BUILD)
        source.forgejo_asset(assets[COMPLETE], self.path / COMPLETE)
        build = json.loads((self.path / BUILD).read_bytes())
        completion = json.loads((self.path / COMPLETE).read_bytes())
        self.sha = build['source_sha']
        require(SHA.fullmatch(self.sha) and tag.endswith('-' + self.sha[:8]) and
                snapshot.commit('refs/tags/' + tag) == self.sha and
                build['version'] == completion['version'] == tag and completion['source_sha'] == self.sha and
                completion['build_sha256'] == build_hash, '验收记录、TAG、SHA 或 build.json 哈希不一致')
        require(isinstance(build['source_branch'], str) and build['source_branch'], '缺少发布来源分支')
        hashes = index(build['packages'], 'name', 'build.json 包清单')
        require(hashes.keys() == package_names(tag) and hashes.keys() <= assets.keys(), '六平台附件清单不匹配')
        self.hashes = {name: package['sha256'] for name, package in hashes.items()}
        require(all(HASH.fullmatch(value) for value in self.hashes.values()), '附件 SHA256 格式无效')
        self.hashes[SUMS] = source.forgejo_asset(assets[SUMS], self.path / SUMS)
        sums = {}
        for line in (self.path / SUMS).read_text().splitlines():
            match = re.fullmatch(r'([0-9a-f]{64}) [ *](.+)', line)
            require(match and match[2] not in sums, 'SHA256SUMS 格式无效或条目重复')
            sums[match[2]] = match[1]
        require(sums == {name: self.hashes[name] for name in hashes}, 'SHA256SUMS 与 build.json 不一致')
        self.hashes[BUILD] = build_hash
        self.image = build['image']
        require(self.image['ref'] == FORGEJO_IMAGE + ':' + tag and
                self.image['platforms'].keys() == set(PLATFORMS) and all(
                    digest.startswith('sha256:') and HASH.fullmatch(digest[7:])
                    for digest in [self.image['digest'], *self.image['platforms'].values()]),
                'OCI 仓库、架构或摘要不符合固定发布契约')
        self.authfile = auth_file()
        self.source_image = FORGEJO_IMAGE + '@' + self.image['digest']
        self.target_image = f'ghcr.io/{GITHUB_OWNER.lower()}/{GITHUB_REPO.lower()}:{tag}'
        verify_image(self.image['ref'], self.image, self.authfile)
        self.image_exists = verify_image(self.target_image, self.image, self.authfile, allow_missing=True)
        self.missing = set(self.hashes)
        if self.target is not None:
            self.check_release(self.target)
            self.target_path = '/releases/' + str(self.target['id'])
            target_assets = index(target.pages(self.target_path + '/assets', '读取 GitHub 附件', size_key='per_page'),
                                  'name', 'GitHub 附件')
            require(target_assets.keys() <= self.hashes.keys(), 'GitHub Release 存在非预期附件')
            for name, asset in target_assets.items():
                require(asset.get('state') == 'uploaded' and
                        target.github_asset(asset, None) == self.hashes[name], 'GitHub 附件内容冲突：' + name)
                self.missing.remove(name)
        for name in sorted(hashes):
            file = self.path / name if name in self.missing else None
            require(source.forgejo_asset(assets[name], file) == self.hashes[name], 'Forgejo 附件 SHA256 不匹配：' + name)

    def check_release(self, release):
        require(release.get('tag_name') == self.tag and release.get('name') == self.title and
                release.get('body') == self.body and release.get('prerelease') is False and
                type(release.get('draft')) is bool, 'GitHub 同名 Release 身份、标题或正文冲突')

    def sync(self, target):
        release = self.target
        if release is None:
            release = target.json('/releases', '创建 GitHub 草稿', method='POST',
                                  payload={'tag_name': self.tag, 'target_commitish': self.sha, 'name': self.title,
                                           'body': self.body, 'draft': True, 'prerelease': False})
            self.check_release(release)
            require(release['draft'] is True, 'GitHub 未返回草稿状态')
            self.target_path = '/releases/' + str(release['id'])
        for name in sorted(self.missing):
            asset = target.upload(release, name, self.path / name)
            require(target.github_asset(asset, None) == self.hashes[name], 'GitHub 上传后 SHA256 不匹配：' + name)
            print(f'{self.tag}：已验证附件 {name}', flush=True)
        if not self.image_exists:
            run(['skopeo', 'copy', '--all', '--preserve-digests', '--authfile', self.authfile,
                 'docker://' + self.source_image, 'docker://' + self.target_image], '复制 GHCR 镜像')
            verify_image(self.target_image, self.image, self.authfile)
            print(f'{self.tag}：已验证 GHCR 多架构镜像', flush=True)
        if release['draft']:
            published = target.json(self.target_path, '发布 GitHub 草稿', method='PATCH',
                                    payload={'draft': False, 'make_latest': 'false'})
            self.check_release(published)
            require(is_formal(published), 'GitHub 未确认正式 Release')
        print(f'{self.tag}：GitHub Release 与镜像已验收', flush=True)


def sync(directory, source, target):
    settings = target.json('', '读取 GitHub 仓库')
    require(settings.get('full_name', '').lower() == f'{GITHUB_OWNER}/{GITHUB_REPO}'.lower() and
            settings.get('private') is (GITHUB_PRIVATE == 'true') and settings.get('archived') is False and
            settings.get('permissions', {}).get('push') is True, 'GitHub 目标、可见性或写权限与接入配置不符')
    require(target.json('/actions/permissions', '核对 GitHub Actions').get('enabled') is False,
            'GitHub Actions 尚未关闭，拒绝任何 GitHub 写入')
    # Forgejo 只向有 Release 写权限的令牌显示草稿；同时要求保留版本形状的裸 TAG 不外推。
    require(source.json('', '读取 Forgejo 仓库').get('permissions', {}).get('push') is True,
            '请使用可查看 Forgejo 草稿的仓库写权限令牌')
    snapshot = Snapshot(directory / 'git')
    dest_refs = snapshot.target()
    releases = index(source.pages('/releases', '读取 Forgejo Release', size_key='limit'), 'tag_name', 'Forgejo Release')
    target_releases = index(target.pages('/releases', '读取 GitHub Release', size_key='per_page'),
                            'tag_name', 'GitHub Release')
    refs = dict(snapshot.heads)
    versions = []
    gaps = []
    for tag in sorted(releases):
        release = releases[tag]
        ref = 'refs/tags/' + tag
        if not is_formal(release):
            print(f'跳过草稿或预发布 TAG：{tag!r}', flush=True)
            continue
        if ref in snapshot.refs and not snapshot.in_scope(ref):
            print(f'跳过未获准公开的 Release：{tag!r}', flush=True)
            continue
        asset_names = index(release['assets'], 'name', 'Forgejo 附件')
        if not VERSION.fullmatch(tag) or COMPLETE not in asset_names:
            gaps.append(tag)
            print(f'未同步历史或未验收正式版本（缺少适用完成证据）：{tag!r}', flush=True)
            continue
        print(f'{tag}：预检 Forgejo 原件与 GitHub 目标', flush=True)
        require(ref in snapshot.refs, 'Forgejo 正式版本缺少对应 TAG；请在发布结束后重跑')
        require(tag not in target_releases or dest_refs.get(ref) == snapshot.refs[ref],
                'GitHub 已有同名 Release，但 TAG 缺失或冲突：' + tag)
        versions.append(Version(tag, release, directory, source, target, snapshot, target_releases.get(tag)))
        refs[ref] = snapshot.refs[ref]
    for ref, sha in sorted(snapshot.refs.items()):
        if not ref.startswith('refs/tags/') or not snapshot.in_scope(ref):
            continue
        tag = ref.removeprefix('refs/tags/')
        if tag not in releases and not VERSION.fullmatch(tag):
            refs[ref] = sha
        elif tag not in releases:
            print(f'跳过尚无验收 Release 的版本 TAG：{tag!r}', flush=True)
    changes = snapshot.changes(refs, dest_refs)
    if USE_LFS == 'true':
        snapshot.lfs(sorted(refs))
    if changes:
        # 不请求强推、镜像或删除。保护规则拒绝个别引用时保留已推送项，重跑会重新核对。
        snapshot.git('push', '--no-force', '--no-mirror', '--no-prune', 'destination', *changes,
                     phase='快进推送 GitHub 引用')
    pushed = snapshot.target()
    require(all(pushed.get(ref) == sha for ref, sha in refs.items()), 'GitHub 引用校验失败')
    print(f'GitHub 引用已验收（更新 {len(changes)} 项）；继续补齐正式产物', flush=True)
    for version in versions:
        print(f'{version.tag}：开始同步正式产物', flush=True)
        version.sync(target)
    require(not gaps, f'仍有 {len(gaps)} 个正式版本未同步；需核实并补录原始发布证据')
    print(f'GitHub 手动同步完成，已验收 {len(versions)} 个正式版本', flush=True)


def main(project):
    global BRANCHES
    values = (FORGEJO_API, FORGEJO_OWNER, FORGEJO_REPO, FORGEJO_GIT, FORGEJO_IMAGE, GITHUB_OWNER, GITHUB_REPO, GITHUB_GIT,
              BRANCHES_JSON)
    require(not any('@@' in value for value in values) and GITHUB_PRIVATE in ('true', 'false') and
            USE_LFS in ('true', 'false'), '同步模板尚未配置')
    BRANCHES = json.loads(BRANCHES_JSON)
    require(isinstance(BRANCHES, list) and all(isinstance(branch, str) and branch for branch in BRANCHES) and
            (BRANCHES == ['*'] or ('main' in BRANCHES and '*' not in BRANCHES)), '分支同步范围无效')
    require(all(re.fullmatch(r'[A-Za-z0-9_.-]+', name) and name not in ('.', '..')
                for name in (FORGEJO_OWNER, FORGEJO_REPO, GITHUB_OWNER, GITHUB_REPO)), '仓库名称配置无效')
    for url in (FORGEJO_GIT, GITHUB_GIT):
        require(not url.startswith('-') and '::' not in url, 'Git 远端地址配置无效')
        if url.startswith('https://'):
            require(not urllib.parse.urlsplit(url).query, 'Git URL 不得携带查询凭据')
            authority(url)
        else:
            require(url.startswith('ssh://') or re.fullmatch(r'[\w.-]+@[\w.-]+:[^\s]+', url),
                    'Git URL 必须使用配置好的 SSH 或 HTTPS')
            if url.startswith('ssh://'):
                require(urllib.parse.urlsplit(url).password is None, 'Git URL 不得携带密码')
    forgejo_token, github_token = os.environ.get('FORGEJO_TOKEN'), os.environ.get('GITHUB_TOKEN')
    require(forgejo_token and github_token, '请显式提供 FORGEJO_TOKEN 和 GITHUB_TOKEN')
    source = API(f'{FORGEJO_API.rstrip("/")}/repos/{FORGEJO_OWNER}/{FORGEJO_REPO}',
                 'token ' + forgejo_token, FORGEJO_DOWNLOAD_HOSTS)
    target = API(f'https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}',
                 'Bearer ' + github_token, GITHUB_DOWNLOAD_HOSTS)
    require(shutil.which('git'), '缺少命令：git')
    runtime = project / 'runtime'
    runtime.mkdir(exist_ok=True)
    fd = os.open(runtime / '.sync-github.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o660)
    with os.fdopen(fd, 'rb') as lock:
        require(stat.S_ISREG(os.fstat(lock.fileno()).st_mode), '同步锁必须为普通文件')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('本项目已有同步任务，请稍后重试') from None
        with tempfile.TemporaryDirectory(prefix='forgejo-sync-') as directory:
            sync(Path(directory), source, target)


if __name__ == '__main__':
    try:
        main(Path(sys.argv[1]))
    except KeyboardInterrupt:
        print('同步中断；已写入的内容保留，核查后重跑以补齐缺口', file=sys.stderr)
        sys.exit(130)
    except RuntimeError as exc:
        print(f'同步失败：{exc}；已完成阶段见前文，核查后重跑', file=sys.stderr)
        sys.exit(1)
    except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError, AttributeError, IndexError):
        print('同步失败：配置、响应数据或 I/O 异常；已完成阶段见前文，核查后重跑', file=sys.stderr)
        sys.exit(1)
PY
