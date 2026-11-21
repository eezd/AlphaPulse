"""同步契约测试：真实临时 Git 引用，HTTP、OCI 和 LFS 不访问外部服务。"""

import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse


ASSET = Path(__file__).parents[2] / 'sysnc-github.sh'
SOURCE = ASSET.read_text().split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
SRC_PATH = '/api/v1/repos/owner/project'
DST_PATH = '/repos/owner/project'
SOURCE_IMAGE = 'forgejo.test/owner/project'
TARGET_IMAGE = 'ghcr.io/owner/project'
TOKENS = {'FORGEJO_TOKEN': 'fixture-forgejo-token', 'GITHUB_TOKEN': 'fixture-github-token'}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def digest(data):
    return 'sha256:' + sha256(data)


class Response(io.BytesIO):
    def __init__(self, body, headers):
        super().__init__(body if isinstance(body, bytes) else json.dumps(body).encode())
        self.headers = headers


class SyncTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source, self.target = (self.root / f'{name}.git' for name in ('source', 'target'))
        self.work, self.project = (self.root / name for name in ('work', 'project'))
        self.project.mkdir()
        self.auth = self.root / 'auth.json'
        self.auth.write_text('{}')
        self.patch_context = contextlib.ExitStack()
        self.addCleanup(self.patch_context.close)
        self.patch_context.enter_context(patch.dict(os.environ, {**TOKENS, 'REGISTRY_AUTH_FILE': str(self.auth),
                                                                 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
                                                                 'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'push.followTags',
                                                                 'GIT_CONFIG_VALUE_0': 'true'}))
        for repo in (self.source, self.target):
            self.git('init', '--bare', str(repo))
        self.git('init', '-b', 'main', str(self.work))
        self.first = self.commit('init')
        self.second = self.commit('fix: second')
        self.git('branch', 'dev')
        self.git('switch', '-c', 'internal')
        self.internal = self.commit('internal')
        self.git('switch', 'main')
        self.git('tag', 'legacy-docs', self.first)
        self.git('tag', 'internal-docs', self.internal)
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                 'tag', '-a', '20261109-ffffffff', '-m', 'pending')
        self.git('tag', '20261109-eeeeeeee')
        self.git('tag', 'preview')
        self.git('tag', '20261109-dddddddd')
        self.releases, self.src_assets, self.dst_releases, self.dst_assets = {}, {}, {}, {}
        self.manifests, self.enabled, self.can_push = {}, False, True
        self.oci_writes, self.http_writes, self.lfs, self.requests = [], [], [], []
        self.failure = None
        self.fail_upload = None
        self.next_id = 100
        self.add_version(self.first)
        self.add_version(self.second)
        for tag, draft, prerelease in [('20261109-eeeeeeee', True, False), ('preview', False, True)]:
            self.releases[tag] = {'id': self.new_id(), 'tag_name': tag, 'draft': draft,
                                  'prerelease': prerelease, 'assets': []}
        self.git('-c', 'push.followTags=false', 'push', self.source, '--all')
        self.git('push', self.source, '--tags')
        self.scope = {'__name__': 'fixture'}
        exec(compile(SOURCE, str(ASSET), 'exec'), self.scope)
        self.scope.update(FORGEJO_API='https://forgejo.test/api/v1', FORGEJO_OWNER='owner', FORGEJO_REPO='project',
                          FORGEJO_GIT=str(self.source), FORGEJO_IMAGE=SOURCE_IMAGE, GITHUB_OWNER='owner',
                          GITHUB_REPO='project', GITHUB_GIT=str(self.target), GITHUB_PRIVATE='false',
                          FORGEJO_DOWNLOAD_HOSTS=('objects.test',), BRANCHES=['main', 'dev'], USE_LFS='false')
        self.original_run = self.scope['run']
        self.scope['run'] = self.run_tool
        self.patch_context.enter_context(patch('urllib.request.build_opener', return_value=self))
        which = self.scope['shutil'].which
        self.patch_context.enter_context(patch('shutil.which', side_effect=lambda p: p if p in ('skopeo', 'git-lfs') else which(p)))
        self.source_api = self.scope['API']('https://forgejo.test' + SRC_PATH, 'token ' + TOKENS['FORGEJO_TOKEN'],
                                            ('objects.test',))
        self.target_api = self.scope['API']('https://api.github.com' + DST_PATH, 'Bearer ' + TOKENS['GITHUB_TOKEN'])

    def git(self, *args, cwd=None):
        workdir = self.work if self.work.exists() else self.root
        return subprocess.run(['git', *args], cwd=cwd or workdir, check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, title):
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '--allow-empty', '-m', title)
        return self.git('rev-parse', 'HEAD')

    def new_id(self):
        self.next_id += 1
        return self.next_id

    def add_version(self, sha):
        tag = '20261108-' + sha[:8]
        self.git('tag', tag, sha)
        packages = {f'project_{tag}_{os_name}_{arch}.{ext}': f'{tag}/{os_name}/{arch}'.encode()
                    for os_name, ext in (('linux', 'tar.gz'), ('darwin', 'tar.gz'), ('windows', 'zip'))
                    for arch in ('amd64', 'arm64')}
        platforms = {}
        manifests = []
        for arch in ('amd64', 'arm64'):
            raw = json.dumps({'schemaVersion': 2, 'architecture': arch}).encode()
            platforms['linux/' + arch] = digest(raw)
            manifests.append({'platform': {'os': 'linux', 'architecture': arch}, 'digest': digest(raw)})
            self.manifests[SOURCE_IMAGE + '@' + digest(raw)] = raw
        top = json.dumps({'schemaVersion': 2, 'manifests': manifests}).encode()
        self.manifests[SOURCE_IMAGE + ':' + tag] = self.manifests[SOURCE_IMAGE + '@' + digest(top)] = top
        build = json.dumps({'version': tag, 'source_branch': 'main', 'source_sha': sha, 'previous_tag': None,
                            'packages': [{'name': name, 'sha256': sha256(body)} for name, body in packages.items()],
                            'image': {'ref': SOURCE_IMAGE + ':' + tag, 'digest': digest(top),
                                      'platforms': platforms}}).encode()
        sums = ''.join(f'{sha256(body)}  {name}\n' for name, body in packages.items()).encode()
        completion = json.dumps({'version': tag, 'source_sha': sha, 'build_sha256': sha256(build)}).encode()
        assets = []
        for name, data in {**packages, 'build.json': build, 'release-complete.json': completion,
                           'SHA256SUMS': sums}.items():
            asset_id = self.new_id()
            self.src_assets[asset_id] = data
            assets.append({'id': asset_id, 'name': name, 'browser_download_url': f'/attachments/{asset_id}'})
        self.releases[tag] = {'id': self.new_id(), 'tag_name': tag, 'name': f'Project {tag}', 'body': 'Original notes',
                              'draft': False, 'prerelease': False, 'assets': assets}

    def run_tool(self, args, phase, **kwargs):
        self.assertFalse(any(token in arg for arg in args for token in TOKENS.values()))
        if args[0] == 'skopeo':
            ref = args[-1].removeprefix('docker://')
            if args[1] == 'inspect':
                content = self.manifests.get(ref)
                result = SimpleNamespace(returncode=0 if content is not None else 2, stdout=content,
                                         stderr=b'manifest unknown' if content is None else b'')
                with patch('subprocess.run', return_value=result):
                    return self.original_run(args, phase, **kwargs)
            self.assertEqual(args[1:4], ['copy', '--all', '--preserve-digests'])
            src = args[-2].removeprefix('docker://')
            self.assertTrue(src.startswith(SOURCE_IMAGE + '@sha256:'))
            top = self.manifests[ref] = self.manifests[src]
            for item in json.loads(top)['manifests']:
                suffix = '@' + item['digest']
                self.manifests[TARGET_IMAGE + suffix] = self.manifests[SOURCE_IMAGE + suffix]
            self.oci_writes.append(ref)
            return None
        if 'lfs' in args and args[0] == 'git':
            self.lfs.append(args[args.index('lfs') + 1:])
            return None
        return self.original_run(args, phase, **kwargs)

    def pages(self, items, query, *, with_total=False):
        page = int(query['page'][0])
        start = (page - 1) * 2  # 模拟服务器把请求页长限制为 2。
        headers = {'X-Total-Count': str(len(items))} if with_total else {}
        if start + 2 < len(items):
            headers['Link'] = '<https://unused.test>; rel="next"'
        return Response(copy.deepcopy(items[start:start + 2]), headers)

    def open(self, request, timeout):
        url = urllib.parse.urlsplit(request.full_url)
        path, query = url.path, urllib.parse.parse_qs(url.query)
        method, host = request.get_method(), url.netloc
        self.requests.append(request)
        expected = {'forgejo.test': 'token ' + TOKENS['FORGEJO_TOKEN'],
                    'api.github.com': 'Bearer ' + TOKENS['GITHUB_TOKEN'],
                    'uploads.github.com': 'Bearer ' + TOKENS['GITHUB_TOKEN'], 'objects.test': None}
        self.assertEqual(request.get_header('Authorization'), expected[host])
        if self.failure and (host, path, query.get('page', [None])[0]) == self.failure:
            raise urllib.error.HTTPError(request.full_url, 403, 'private reason', {},
                                         io.BytesIO(str(TOKENS).encode()))
        if method != 'GET':
            self.http_writes.append((method, path))
        if host == 'forgejo.test':
            if path == SRC_PATH:
                return Response({'permissions': {'push': True}}, {})
            if path == SRC_PATH + '/releases':
                return self.pages(list(self.releases.values()), query, with_total=True)
            if path.startswith('/attachments/'):
                raise urllib.error.HTTPError(request.full_url, 302, 'attachment storage',
                                             {'Location': 'https://objects.test' + path}, io.BytesIO())
        if host == 'objects.test':
            return Response(self.src_assets[int(path.rsplit('/', 1)[-1])], {})
        if path == DST_PATH:
            return Response({'full_name': 'Owner/Project', 'private': False, 'archived': False,
                             'permissions': {'push': self.can_push}}, {})
        if path == DST_PATH + '/actions/permissions':
            return Response({'enabled': self.enabled}, {})
        if path == DST_PATH + '/releases':
            if method == 'GET':
                return self.pages(list(self.dst_releases.values()), query)
            new_id = self.new_id()
            release = {**json.loads(request.data), 'id': new_id,
                       'upload_url': f'https://uploads.github.com{DST_PATH}/releases/{new_id}/assets{{?name,label}}'}
            self.dst_releases[new_id] = release
            return Response(copy.deepcopy(release), {})
        if '/releases/assets/' in path:
            return Response(self.dst_assets[int(path.rsplit('/', 1)[-1])]['content'], {})
        if path.endswith('/assets'):
            release_id = int(path.split('/')[-2])
            if method == 'GET':
                assets = [{key: value for key, value in asset.items() if key != 'content'}
                          for asset in self.dst_assets.values() if asset['release_id'] == release_id]
                return self.pages(assets, query)
            name = query['name'][0]
            if self.fail_upload == name:
                raise urllib.error.HTTPError(request.full_url, 403, 'private reason', {}, io.BytesIO(b'private body'))
            asset_id = self.new_id()
            asset = {'id': asset_id, 'state': 'uploaded', 'name': name, 'release_id': release_id}
            self.dst_assets[asset_id] = {**asset, 'content': request.data.read()}
            return Response(asset, {})
        if method == 'PATCH':
            release = self.dst_releases[int(path.rsplit('/', 1)[-1])]
            self.assertEqual(json.loads(request.data), {'draft': False, 'make_latest': 'false'})
            self.assertIn(TARGET_IMAGE + ':' + release['tag_name'], self.manifests)
            self.assertEqual(sum(a['release_id'] == release['id'] for a in self.dst_assets.values()), 8)
            release['draft'] = False
            return Response(copy.deepcopy(release), {})
        raise AssertionError((host, path, method))

    def sync(self):
        with tempfile.TemporaryDirectory(dir=self.root) as directory, contextlib.redirect_stdout(io.StringIO()) as out:
            try:
                self.scope['sync'](Path(directory), self.source_api, self.target_api)
            finally:
                self.output = out.getvalue()

    def refs(self):
        return set(self.git('--git-dir=' + str(self.target), 'for-each-ref', '--format=%(refname)').splitlines())

    def test_batch_then_no_writes(self):
        self.scope['USE_LFS'] = 'true'
        self.sync()
        tags = {tag for tag, release in self.releases.items() if not release['draft'] and not release['prerelease']}
        refs = {'refs/heads/main', 'refs/heads/dev', 'refs/tags/legacy-docs', *('refs/tags/' + tag for tag in tags)}
        self.assertEqual(self.refs(), refs)
        self.assertEqual(self.lfs[:2], [['fetch', '--all', 'source', *sorted(refs)],
                                        ['push', '--all', 'destination', *sorted(refs)]])
        self.assertEqual(len(self.oci_writes), 2)
        self.assertEqual(len(self.dst_assets), 16)
        self.assertFalse(any(release['draft'] for release in self.dst_releases.values()))
        self.assertNotIn('release-complete.json', {asset['name'] for asset in self.dst_assets.values()})
        self.assertIn('跳过尚无验收 Release 的版本 TAG', self.output)
        self.http_writes.clear()
        self.oci_writes.clear()
        self.sync()
        self.assertEqual(self.http_writes, [])
        self.assertEqual(self.oci_writes, [])
        self.assertIn('更新 0 项', self.output)

    def test_fast_forward_leaves_target_only_ref_and_excludes_dev(self):
        self.scope['BRANCHES'] = ['main']
        self.git('-c', 'push.followTags=false', 'push', self.target,
                 f'{self.first}:refs/heads/main', 'internal:refs/heads/github-only')
        self.sync()
        self.assertEqual(self.git('--git-dir=' + str(self.target), 'rev-parse', 'main'), self.second)
        self.assertIn('refs/heads/github-only', self.refs())
        self.assertNotIn('refs/heads/dev', self.refs())

    def test_retry_recovers_verified_draft(self):
        first_tag = min(tag for tag, release in self.releases.items() if not release['draft'] and not release['prerelease'])
        self.fail_upload = f'project_{first_tag}_darwin_arm64.tar.gz'
        with self.assertRaisesRegex(RuntimeError, '上传 GitHub 附件失败：HTTP 403'):
            self.sync()
        self.assertIn('已验证附件', self.output)
        self.assertEqual(len(self.dst_releases), 1)
        self.assertTrue(next(iter(self.dst_releases.values()))['draft'])
        previous_assets = set(self.dst_assets)
        self.fail_upload = None
        self.sync()
        self.assertTrue(previous_assets <= self.dst_assets.keys())
        self.assertEqual(len(self.dst_assets), 16)
        self.assertEqual(len(self.dst_releases), 2)
        self.assertFalse(any(release['draft'] for release in self.dst_releases.values()))

    def test_release_asset_and_oci_conflict_never_overwrite(self):
        self.sync()
        self.http_writes.clear()
        self.oci_writes.clear()
        asset = next(iter(self.dst_assets.values()))
        old, asset['content'] = asset['content'], b'wrong content'
        with self.assertRaisesRegex(RuntimeError, 'GitHub 附件内容冲突'):
            self.sync()
        asset['content'] = old
        image_ref = next(ref for ref in self.manifests if ref.startswith(TARGET_IMAGE + ':'))
        self.manifests[image_ref] = b'wrong manifest'
        with self.assertRaisesRegex(RuntimeError, 'OCI 顶层摘要冲突'):
            self.sync()
        self.assertEqual((self.http_writes, self.oci_writes), ([], []))

    def test_divergent_branch_and_tag_conflicts_precede_writes(self):
        self.git('-c', 'push.followTags=false', 'push', self.target, 'internal:refs/heads/main')
        with self.assertRaisesRegex(RuntimeError, '分支分叉或 TAG 冲突：refs/heads/main'):
            self.sync()
        self.git('-c', 'push.followTags=false', 'push', self.target, ':refs/heads/main', 'internal:refs/tags/legacy-docs')
        with self.assertRaisesRegex(RuntimeError, '分支分叉或 TAG 冲突：refs/tags/legacy-docs'):
            self.sync()
        self.assertEqual(self.refs(), {'refs/tags/legacy-docs'})
        self.assertEqual((self.http_writes, self.oci_writes, self.lfs), ([], [], []))

    def test_actions_permission_and_later_page_failure_cause_zero_writes(self):
        self.scope['USE_LFS'] = 'true'
        self.enabled = True
        with self.assertRaisesRegex(RuntimeError, 'GitHub Actions 尚未关闭'):
            self.sync()
        self.enabled, self.can_push = False, False
        with self.assertRaisesRegex(RuntimeError, 'GitHub 目标、可见性或写权限'):
            self.sync()
        self.can_push = True
        self.failure = ('forgejo.test', SRC_PATH + '/releases', '2')
        with self.assertRaisesRegex(RuntimeError, '读取 Forgejo Release失败：HTTP 403') as error:
            self.sync()
        self.assertNotIn('fixture-', str(error.exception) + self.output)
        self.assertEqual(self.refs(), set())
        self.assertEqual((self.http_writes, self.oci_writes, self.lfs), ([], [], []))

    def test_registry_inspect_classifies_missing_destination_only(self):
        cases = ((1, b'name unknown', True, True), (2, b'manifest unknown', True, True),
                 (2, b'manifest unknown', False, False), (2, b'unauthorized', True, False),
                 (2, b'toomanyrequests', True, False))
        for status, error, allow_missing, absent in cases:
            result = SimpleNamespace(returncode=status, stdout=b'', stderr=error)
            with patch.dict(self.scope, run=self.original_run), patch('subprocess.run', return_value=result):
                with self.subTest(status=status, error=error, allow_missing=allow_missing):
                    args = (TARGET_IMAGE + ':fixture', str(self.auth))
                    if absent:
                        self.assertIsNone(self.scope['read_manifest'](*args, allow_missing=allow_missing))
                    else:
                        with self.assertRaisesRegex(RuntimeError, '读取 OCI 清单失败'):
                            self.scope['read_manifest'](*args, allow_missing=allow_missing)

    def test_unproven_formal_release_is_reported_and_tag_never_pushed(self):
        tag = '20261109-dddddddd'
        self.releases[tag] = {'id': self.new_id(), 'tag_name': tag, 'draft': False, 'prerelease': False, 'assets': []}
        with self.assertRaisesRegex(RuntimeError, '仍有 1 个正式版本未同步'):
            self.sync()
        self.assertIn(tag, self.output)
        self.assertNotIn('refs/tags/' + tag, self.refs())
        self.assertEqual(len(self.dst_releases), 2)

    def test_git_subcommands_do_not_receive_api_credentials(self):
        result = self.original_run(['bash', '-c', '[[ ! -v FORGEJO_TOKEN && ! -v GITHUB_TOKEN ]]'], '模拟 Git')
        self.assertEqual(result.returncode, 0)

    def test_incomplete_pagination_detected_from_headers(self):
        with patch.object(self.source_api, 'open', side_effect=[Response([{'id': 1}], {'X-Total-Count': '2'}),
                                                               Response([], {'X-Total-Count': '2'})]):
            with self.assertRaisesRegex(RuntimeError, '分页结果不完整'):
                self.source_api.pages('/releases', '模拟分页', size_key='limit')

    def test_launch_from_other_cwd_requires_explicit_credentials_without_network(self):
        env = {key: value for key, value in os.environ.items() if key not in TOKENS}
        result = subprocess.run([str(ASSET)], cwd=self.project, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('FORGEJO_TOKEN', result.stderr)
        self.assertNotIn('root', result.stderr)
