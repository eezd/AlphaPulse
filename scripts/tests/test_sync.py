"""同步契约测试：真实临时 Git 引用，HTTP、OCI 和 LFS 不访问外部服务。"""

import contextlib
import copy
import hashlib
import io
import importlib.util
import json
import os
import re
import stat
import sys
from pathlib import Path
import subprocess
import tempfile
import textwrap
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.parse


ASSET = Path(__file__).parents[2] / 'scripts/sync-github.py'


def load_controller():
    spec = importlib.util.spec_from_file_location('sync_github', ASSET)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


class CredentialTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.auth = self.root / 'alphapulse-sync-42-2'
        environment = patch.dict(os.environ, {
            'HOME': str(self.home), **TOKENS, 'CONTROL_REF': 'refs/heads/main',
            'CI_RUN_ID': '42', 'CI_RUN_ATTEMPT': '2', 'RUNNER_TEMP': str(self.root),
            'CI_AUTH_DIR': str(self.auth),
        }, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.module = load_controller()
        self.api = Mock()
        self.api.return_value.json.return_value = {'login': 'eezd'}
        self.run_sync = Mock()
        self.output = io.StringIO()
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(self.module, 'API', self.api))
        self.patches.enter_context(patch.object(self.module, 'sync', self.run_sync))
        self.patches.enter_context(patch.object(self.module.shutil, 'which', return_value='/fixture/tool'))

    def invoke(self):
        with contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.output):
            self.module.main()

    def assert_preflight_rejected(self, message):
        self.api.reset_mock()
        with self.assertRaisesRegex(RuntimeError, message):
            self.invoke()
        self.api.assert_not_called()
        self.run_sync.assert_not_called()
        self.assertFalse(self.auth.exists())

    def test_missing_or_empty_secret_never_reads_home_or_calls_api(self):
        for service, variable in (('forgejo', 'FORGEJO_TOKEN'), ('github', 'GITHUB_TOKEN')):
            path = self.home / '.config' / service / 'token'
            path.parent.mkdir(parents=True)
            path.write_text('must-not-use-home-token')
            for value in (None, '', ' \n\t'):
                with self.subTest(variable=variable, value=value), patch.dict(os.environ):
                    if value is None:
                        os.environ.pop(variable)
                    else:
                        os.environ[variable] = value
                    with patch.object(Path, 'read_text', side_effect=AssertionError('HOME fallback')):
                        self.assert_preflight_rejected(variable)
        self.assertNotIn('must-not-use-home-token', self.output.getvalue())

    def test_untrusted_ref_or_invalid_run_rejected_before_api_or_writes(self):
        for key, values in (
                ('CONTROL_REF', ('', 'refs/heads/dev', 'refs/tags/main')),
                ('CI_RUN_ID', ('', '0', '-1', '1.5', '01', '1/../../x')),
                ('CI_RUN_ATTEMPT', ('', '0', '-1', 'abc'))):
            for value in values:
                with self.subTest(key=key, value=value), patch.dict(os.environ, {key: value}):
                    self.assert_preflight_rejected('控制代码|正整数')

    def test_auth_directory_must_be_new_direct_child_with_exact_run_name(self):
        sibling = self.root / 'other'
        sibling.mkdir()
        link = self.root / 'linked'
        link.symlink_to(sibling, target_is_directory=True)
        for path in (self.root, self.root / 'alphapulse-sync-42-1',
                     sibling / 'alphapulse-sync-42-2', link, Path('alphapulse-sync-42-2')):
            with self.subTest(path=path), patch.dict(os.environ, {'CI_AUTH_DIR': str(path)}):
                self.assert_preflight_rejected('CI_AUTH_DIR')
        self.auth.symlink_to(sibling, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'CI_AUTH_DIR'):
            self.invoke()
        self.assertTrue(sibling.is_dir())
        self.api.assert_not_called()
        self.auth.unlink()
        self.auth.mkdir()
        sentinel = self.auth / 'do-not-delete'
        sentinel.touch()
        with self.assertRaisesRegex(RuntimeError, 'CI_AUTH_DIR'):
            self.invoke()
        self.assertTrue(sentinel.exists())

    def test_default_attempt_is_one(self):
        with patch.dict(os.environ):
            os.environ.pop('CI_RUN_ATTEMPT')
            os.environ['CI_AUTH_DIR'] = str(self.root / 'alphapulse-sync-42-1')
            self.assertEqual(self.module.ci_auth_directory(), self.root / 'alphapulse-sync-42-1')

    def test_invalid_configuration_precedes_api_and_files(self):
        for key, value in (('GITHUB_GIT', 'git@github.com:eezd/AlphaPulse.git'),
                           ('FORGEJO_GIT', 'https://other.test/eezd/AlphaPulse.git'),
                           ('GITHUB_GIT', 'https://github.com/eezd/AlphaPulse.git?token=secret'),
                           ('BRANCHES_JSON', '["dev"]'), ('USE_LFS', 'maybe')):
            with self.subTest(key=key), patch.object(self.module, key, value):
                self.assert_preflight_rejected('HTTPS|范围|未配置')

    def test_wrong_api_identity_stops_before_auth_files_and_sync(self):
        for identities in (({'login': 'wrong'}, {'login': 'eezd'}),
                           ({'login': 'eezd'}, {'login': 'wrong'})):
            self.api.return_value.json.side_effect = identities
            with self.assertRaisesRegex(RuntimeError, 'eezd'):
                self.invoke()
            self.run_sync.assert_not_called()
            self.assertFalse(self.auth.exists())

    def test_registry_login_uses_verified_identity_stdin_and_cleanup(self):
        calls = []

        def subprocess_run(args, **kwargs):
            calls.append((args, kwargs))
            self.assertEqual(stat.S_IMODE(self.auth.stat().st_mode), 0o700)
            for token in TOKENS.values():
                self.assertNotIn(token, str(args))
                self.assertNotIn(token, str(kwargs['env']))
            self.assertEqual(args[:2], ['skopeo', 'login'])
            self.assertIn('--password-stdin', args)
            self.assertEqual(args[args.index('--username') + 1], 'eezd')
            self.auth.joinpath('registry.json').write_text('{}')
            return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')

        def syncing(*args):
            self.assertEqual(args[-1], str(self.auth / 'registry.json'))
            self.assertEqual(stat.S_IMODE((self.auth / 'registry.json').stat().st_mode), 0o600)
            for name in ('source.credentials', 'destination.credentials', 'gitconfig'):
                self.assertEqual(stat.S_IMODE((self.auth / name).stat().st_mode), 0o600)

        self.run_sync.side_effect = syncing
        with patch('subprocess.run', side_effect=subprocess_run):
            self.invoke()
        self.assertEqual([call[0][-1] for call in calls], ['git.134333.xyz', 'ghcr.io'])
        self.assertEqual([call[1]['input'] for call in calls],
                         [(TOKENS['FORGEJO_TOKEN'] + '\n').encode(), (TOKENS['GITHUB_TOKEN'] + '\n').encode()])
        self.assertEqual([c.args for c in self.api.return_value.json.call_args_list],
                         [('/user', '核对 Forgejo 身份'), ('/user', '核对 GitHub 身份')])
        self.run_sync.assert_called_once()
        self.assertFalse(self.auth.exists())

    def test_auth_cleanup_on_registry_and_sync_exceptions(self):
        for stage in ('git', 'registry', 'sync'):
            with self.subTest(stage=stage):
                self.run_sync.reset_mock()
                self.run_sync.side_effect = RuntimeError('sync failure') if stage == 'sync' else None
                with patch.object(self.module.Runtime, 'configure_git', side_effect=(
                        RuntimeError('git failure') if stage == 'git' else None)), \
                        patch.object(self.module.Runtime, 'login_registry', side_effect=(
                            RuntimeError('registry failure') if stage == 'registry' else None)), \
                        self.assertRaises(RuntimeError):
                    self.invoke()
                self.assertFalse(self.auth.exists())
        for token in TOKENS.values():
            self.assertNotIn(token, self.output.getvalue())

    def test_git_credentials_exact_urls_no_host_helper_or_config(self):
        self.auth.mkdir(mode=0o700)
        runtime = self.module.Runtime(self.auth)
        runtime.configure_git('eezd', 'eezd', TOKENS['FORGEJO_TOKEN'], TOKENS['GITHUB_TOKEN'])
        # 如果继承宿主配置，任意 URL 都会取得此伪凭据，且 hooks 设置也会改变。
        (self.home / '.gitconfig').write_text(
            '[credential]\n helper = !echo username=host; echo password=host-secret\n'
            '[core]\n hooksPath = /host/hooks\n')
        with patch.dict(os.environ, {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'credential.helper',
                                     'GIT_CONFIG_VALUE_0': '!echo password=inherited-secret',
                                     'GIT_ASKPASS': '/must-not-run', 'REGISTRY_AUTH_FILE': '/host/auth.json'}):
            for url, token in ((self.module.FORGEJO_GIT, TOKENS['FORGEJO_TOKEN']),
                               (self.module.GITHUB_GIT, TOKENS['GITHUB_TOKEN'])):
                result = runtime.run(['git', 'credential', 'fill'], '读取测试凭据',
                                     input=f'url={url}\n\n'.encode())
                self.assertIn(('password=' + token).encode(), result.stdout)
                for other in (url + '/extra', url.replace('AlphaPulse.git', 'Other.git'),
                              url.replace('/eezd/', '/other/'), url.replace('https:', 'http:'),
                              url.replace('https://', 'https://other.')):
                    denied = runtime.run(['git', 'credential', 'fill'], '拒绝非目标 URL',
                                         input=f'url={other}\n\n'.encode(), allowed=(128,))
                    self.assertFalse(denied.stdout)
            result = runtime.run(['git', 'config', '--get', 'core.hooksPath'], '核对 hooks')
            self.assertEqual(result.stdout.strip(), b'/dev/null')

    def test_runtime_never_inherits_tokens_home_or_git_environment(self):
        runtime = self.module.Runtime(self.auth)
        with patch.dict(os.environ, {'FORGEJO_RELEASE_TOKEN': 'another-secret', 'GIT_DIR': '/host/repo',
                                     'GIT_CONFIG_PARAMETERS': "'credential.helper=host'"}), \
                patch('subprocess.run', return_value=SimpleNamespace(returncode=1)) as proc:
            with self.assertRaisesRegex(RuntimeError, '退出码 1') as error:
                runtime.run(['git', 'ls-remote', self.module.GITHUB_GIT], '模拟 Git')
            environment = proc.call_args.kwargs['env']
            self.assertEqual(proc.call_args.kwargs['cwd'], self.auth)
            self.assertEqual(environment['HOME'], str(self.auth))
            self.assertEqual(environment['GIT_CONFIG_GLOBAL'], str(runtime.config))
            for key in ('FORGEJO_TOKEN', 'GITHUB_TOKEN', 'FORGEJO_RELEASE_TOKEN', 'GIT_DIR',
                        'GIT_CONFIG_COUNT', 'GIT_CONFIG_PARAMETERS'):
                self.assertNotIn(key, environment)
            for token in TOKENS.values():
                self.assertNotIn(token, str(error.exception) + str(proc.call_args))


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
        self.patch_context.enter_context(patch.dict(os.environ, {**TOKENS, 'HOME': str(self.root / 'home'),
                                                                 'REGISTRY_AUTH_FILE': str(self.auth),
                                                                 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
                                                                 'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'push.followTags',
                                                                 'GIT_CONFIG_VALUE_0': 'true'}, clear=True))
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
        self.module = load_controller()
        self.scope = self.module.__dict__
        self.scope.update(FORGEJO_API='https://forgejo.test/api/v1', FORGEJO_OWNER='owner', FORGEJO_REPO='project',
                          FORGEJO_GIT=str(self.source), FORGEJO_IMAGE=SOURCE_IMAGE, GITHUB_OWNER='owner',
                          GITHUB_REPO='project', GITHUB_GIT=str(self.target), GITHUB_PRIVATE='false',
                          FORGEJO_DOWNLOAD_HOSTS=('objects.test',), BRANCHES=['main', 'dev'], USE_LFS='false')
        (self.root / 'private-auth').mkdir(mode=0o700)
        self.original_run = self.scope['Runtime'](self.root / 'private-auth').run
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

    def add_version(self, sha, branch='main'):
        tag = '20261108-' + sha[:8]
        self.git('tag', tag, sha)
        packages = {f'project_{tag}_{os_name}_{arch}.{ext}': f'{tag}/{os_name}/{arch}'.encode()
                    for os_name, ext in (('linux', 'tar.gz'), ('darwin', 'tar.gz'), ('windows', 'zip'))
                    for arch in ('amd64', 'arm64')}
        platforms = {}
        manifests = []
        for arch in ('amd64', 'arm64'):
            raw = json.dumps({'schemaVersion': 2, 'architecture': arch, 'revision': sha}).encode()
            platforms['linux/' + arch] = digest(raw)
            manifests.append({'platform': {'os': 'linux', 'architecture': arch}, 'digest': digest(raw)})
            self.manifests[SOURCE_IMAGE + '@' + digest(raw)] = raw
        top = json.dumps({'schemaVersion': 2, 'manifests': manifests}).encode()
        self.manifests[SOURCE_IMAGE + ':' + tag] = self.manifests[SOURCE_IMAGE + '@' + digest(top)] = top
        build = json.dumps({'version': tag, 'source_branch': branch, 'source_sha': sha, 'previous_tag': None,
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
            mode = '--multi-arch=index-only' if ref == SOURCE_IMAGE + ':latest' else '--all'
            self.assertEqual(args[1:4], ['copy', mode, '--preserve-digests'])
            src = args[-2].removeprefix('docker://')
            self.assertTrue(src.startswith(SOURCE_IMAGE + '@sha256:'))
            top = self.manifests[ref] = self.manifests[src]
            if mode == '--all':
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
                self.scope['sync'](Path(directory), self.source_api, self.target_api, str(self.auth))
            finally:
                self.output = out.getvalue()

    def refs(self):
        return set(self.git('--git-dir=' + str(self.target), 'for-each-ref', '--format=%(refname)').splitlines())

    def readable_versions(self):
        for record in self.releases.values():
            if record['draft'] or record['prerelease']:
                continue
            assets = {asset['name']: asset['id'] for asset in record['assets']}
            build = json.loads(self.src_assets[assets['build.json']])
            image = build['image']
            image['architecture_tags'] = {platform: record['tag_name']+'-'+platform.split('/')[1]
                                           for platform in image['platforms']}
            for platform, tag in image['architecture_tags'].items():
                self.manifests[SOURCE_IMAGE+':'+tag] = self.manifests[SOURCE_IMAGE+'@'+image['platforms'][platform]]
            raw = json.dumps(build).encode()
            self.src_assets[assets['build.json']] = raw
            self.src_assets[assets['release-complete.json']] = json.dumps({
                'version': build['version'], 'source_sha': build['source_sha'], 'build_sha256': sha256(raw)}).encode()

    def test_readable_source_tags_are_verified_without_architecture_aliases_on_ghcr(self):
        self.readable_versions()
        self.sync()
        self.assertFalse(any(ref.endswith(('-amd64', '-arm64')) for ref in self.oci_writes))
        self.assertEqual(len(self.oci_writes), 4)
        self.oci_writes.clear()
        self.sync()
        self.assertEqual(self.oci_writes, [])

    def test_missing_or_swapped_source_architecture_tag_stops_before_writes(self):
        self.readable_versions()
        tag = '20261108-'+self.first[:8]
        for arch in ('amd64', 'arm64'):
            ref = SOURCE_IMAGE+':'+tag+'-'+arch
            raw = self.manifests.pop(ref)
            try:
                with self.subTest(arch=arch), self.assertRaisesRegex(RuntimeError, '读取 OCI 清单失败'):
                    self.sync()
                self.assertEqual((self.http_writes, self.oci_writes, self.refs()), ([], [], set()))
            finally:
                self.manifests[ref] = raw
        self.manifests[SOURCE_IMAGE+':'+tag+'-arm64'] = self.manifests[SOURCE_IMAGE+':'+tag+'-amd64']
        with self.assertRaisesRegex(RuntimeError, '架构标签摘要冲突'):
            self.sync()
        self.assertEqual((self.http_writes, self.oci_writes, self.refs()), ([], [], set()))

    def test_anonymous_source_child_tag_denied_stops_before_writes(self):
        self.readable_versions()
        def private_tag(args, phase, **kwargs):
            if kwargs.get('anonymous_home') and args[-1].endswith('-arm64'):
                result = SimpleNamespace(returncode=2, stdout=b'', stderr=b'unauthorized')
                with patch('subprocess.run', return_value=result):
                    return self.original_run(args, phase, **kwargs)
            return self.run_tool(args, phase, **kwargs)
        with patch.dict(self.scope, run=private_tag), self.assertRaisesRegex(RuntimeError, 'Forgejo 来源版本公开分发验收失败') as failure:
            self.sync()
        self.assertIsNotNone(failure.exception.__cause__)
        self.assertNotIn('Change visibility', str(failure.exception))
        self.assertEqual((self.http_writes, self.oci_writes, self.refs()), ([], [], set()))

    def test_anonymous_ghcr_arm64_denied_does_not_complete_target_release(self):
        def private_child(args, phase, **kwargs):
            if kwargs.get('anonymous_home') and args[-1].startswith('docker://'+TARGET_IMAGE+'@'):
                result = SimpleNamespace(returncode=2, stdout=b'', stderr=b'unauthorized')
                with patch('subprocess.run', return_value=result):
                    return self.original_run(args, phase, **kwargs)
            return self.run_tool(args, phase, **kwargs)
        with patch.dict(self.scope, run=private_child), self.assertRaisesRegex(RuntimeError, 'GHCR 版本公开分发验收失败') as failure:
            self.sync()
        message = str(failure.exception)
        for expected in ('公开分发待验收', TARGET_IMAGE, 'https://github.com/owner?tab=packages',
                         '若该包仍为 private', 'Change visibility → Public', '复用已上传原件、只补缺项'):
            self.assertIn(expected, message)
        self.assertNotIn('unauthorized', message + self.output)
        self.assertIsNotNone(failure.exception.__cause__)
        self.assertTrue(all(record['draft'] for record in self.dst_releases.values()))
        self.assertFalse(any(method == 'PATCH' for method, _ in self.http_writes))
        self.assertFalse(any(ref.endswith(':latest') for ref in self.oci_writes))
        assets, copies = set(self.dst_assets), list(self.oci_writes)
        self.http_writes.clear()
        self.oci_writes.clear()
        self.sync()
        self.assertTrue(assets <= self.dst_assets.keys())
        self.assertTrue(set(copies).isdisjoint(self.oci_writes))
        self.assertEqual(len(self.dst_assets), 16)
        self.assertFalse(any(record['draft'] for record in self.dst_releases.values()))

    def test_anonymous_latest_failure_never_prints_acceptance(self):
        self.sync()
        self.oci_writes.clear()
        for repository, label in ((SOURCE_IMAGE, 'Forgejo'), (TARGET_IMAGE, 'GHCR')):
            def private_latest(args, phase, repository=repository, **kwargs):
                if kwargs.get('anonymous_home') and args[-1] == 'docker://'+repository+':latest':
                    result = SimpleNamespace(returncode=2, stdout=b'', stderr=b'synthetic-secret denied')
                    with patch('subprocess.run', return_value=result):
                        return self.original_run(args, phase, **kwargs)
                return self.run_tool(args, phase, **kwargs)
            with self.subTest(registry=label), patch.dict(self.scope, run=private_latest), \
                    self.assertRaisesRegex(RuntimeError, label+' latest 公开分发验收失败'):
                self.sync()
            self.assertNotIn('两端 OCI latest 已验收', self.output)
            self.assertEqual(self.oci_writes, [])

    def test_anonymous_failure_preserves_cause_without_leaking_transport_text(self):
        cause = ValueError('synthetic-secret malformed manifest')
        verifier = Mock(side_effect=cause)
        with patch.dict(self.scope, verify_image=verifier), \
                self.assertRaisesRegex(RuntimeError, 'GHCR 版本公开分发验收失败') as failure:
            self.scope['verify_public_image'](TARGET_IMAGE+':fixture', {}, phase='GHCR 版本公开分发验收')
        self.assertIs(failure.exception.__cause__, cause)
        self.assertNotIn('synthetic-secret', str(failure.exception))
        self.assertIn('清单摘要', str(failure.exception))
        self.assertIn('若该包仍为 private', str(failure.exception))
        directory = Path(verifier.call_args.kwargs['anonymous_home'])
        self.assertFalse(directory.exists())

    def test_anonymous_runtime_ignores_logged_in_registry_and_home(self):
        with tempfile.TemporaryDirectory(dir=self.root) as home:
            result = self.original_run(['sh', '-c', 'printf "%s\\n" "$HOME" "$DOCKER_CONFIG" "$REGISTRY_AUTH_FILE" "$GIT_CONFIG_GLOBAL"'],
                                       '模拟匿名环境', anonymous_home=home)
        self.assertEqual(result.stdout.decode().splitlines(), [home, home, home+'/registry.json', '/dev/null'])

    def test_batch_then_no_writes(self):
        self.scope['USE_LFS'] = 'true'
        self.sync()
        tags = {tag for tag, release in self.releases.items() if not release['draft'] and not release['prerelease']}
        refs = {'refs/heads/main', 'refs/heads/dev', 'refs/tags/legacy-docs', *('refs/tags/' + tag for tag in tags)}
        self.assertEqual(self.refs(), refs)
        self.assertEqual(self.lfs[:2], [['fetch', '--all', 'source', *sorted(refs)],
                                        ['push', '--all', 'destination', *sorted(refs)]])
        self.assertEqual(set(self.oci_writes), {SOURCE_IMAGE + ':latest', TARGET_IMAGE + ':latest',
                         *(TARGET_IMAGE + ':20261108-' + sha[:8] for sha in (self.first, self.second))})
        self.assertEqual(self.manifests[SOURCE_IMAGE + ':latest'], self.manifests[SOURCE_IMAGE + ':20261108-' + self.second[:8]])
        self.assertEqual(self.manifests[TARGET_IMAGE + ':latest'], self.manifests[SOURCE_IMAGE + ':latest'])
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

    def test_latest_ignores_dev_and_historical_iteration_order(self):
        self.git('switch', 'dev')
        newer_dev = self.commit('new dev')
        self.add_version(newer_dev, branch='dev')
        self.git('-c', 'push.followTags=false', 'push', self.source, '--all')
        self.git('push', self.source, '--tags')
        self.releases = dict(reversed(list(self.releases.items())))
        self.sync()
        expected = self.manifests[SOURCE_IMAGE + ':20261108-' + self.second[:8]]
        self.assertEqual(self.manifests[SOURCE_IMAGE + ':latest'], expected)
        self.assertEqual(self.manifests[TARGET_IMAGE + ':latest'], expected)
        self.oci_writes.clear()
        self.sync()
        self.assertEqual(self.oci_writes, [])
        self.assertNotIn('refs/tags/latest', self.refs())

    def test_unknown_latest_stops_before_any_target_write(self):
        for repository in (SOURCE_IMAGE, TARGET_IMAGE):
            with self.subTest(repository=repository):
                self.manifests[repository + ':latest'] = b'unknown'
                with self.assertRaisesRegex(RuntimeError, 'latest'):
                    self.sync()
                self.assertEqual(self.http_writes, [])
                self.assertEqual(self.oci_writes, [])
                self.assertEqual(self.refs(), set())
                del self.manifests[repository + ':latest']

    def test_latest_ancestry_conflict_stops_before_writes(self):
        self.git('switch', '-c', 'fork', self.first)
        fork = self.commit('fork')
        self.add_version(fork)
        self.git('switch', 'main')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                 'merge', '--no-ff', 'fork', '-m', 'merge')
        self.git('-c', 'push.followTags=false', 'push', self.source, '--all')
        self.git('push', self.source, '--tags')
        with self.assertRaisesRegex(RuntimeError, '唯一'):
            self.sync()
        self.assertEqual(self.http_writes, [])
        self.assertEqual(self.oci_writes, [])

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

    def test_api_success_but_git_transport_failure_precedes_target_writes(self):
        for side in ('source', 'destination'):
            def fail_git(args, phase, failed_side=side, **kwargs):
                if args[0] == 'git' and failed_side in args and any(
                        command in args for command in ('fetch', 'ls-remote')):
                    result = SimpleNamespace(returncode=128, stdout=b'', stderr=str(TOKENS).encode())
                    with patch('subprocess.run', return_value=result):
                        return self.original_run(args, phase, **kwargs)
                return self.run_tool(args, phase, **kwargs)

            with self.subTest(side=side), patch.dict(self.scope, run=fail_git):
                with self.assertRaisesRegex(RuntimeError, '退出码 128') as error:
                    self.sync()
                for token in TOKENS.values():
                    self.assertNotIn(token, str(error.exception) + self.output)
                self.assertEqual(self.refs(), set())
                self.assertEqual((self.http_writes, self.oci_writes, self.lfs), ([], [], []))

    def test_bad_source_package_hash_precedes_target_writes(self):
        release = next(iter(self.releases.values()))
        asset = next(a for a in release['assets'] if a['name'].endswith('.tar.gz'))
        self.src_assets[asset['id']] = b'changed source package'
        with self.assertRaisesRegex(RuntimeError, 'Forgejo 附件 SHA256 不匹配'):
            self.sync()
        self.assertEqual(self.refs(), set())
        self.assertEqual((self.http_writes, self.oci_writes, self.lfs), ([], [], []))

    def test_unapproved_release_and_tag_are_not_mirrored(self):
        self.add_version(self.internal)
        tag = '20261108-' + self.internal[:8]
        self.git('push', self.source, 'refs/tags/' + tag)
        self.sync()
        self.assertIn('跳过未获准公开的 Release', self.output)
        self.assertNotIn('refs/tags/' + tag, self.refs())
        self.assertNotIn(tag, {release['tag_name'] for release in self.dst_releases.values()})
        self.assertEqual(len(self.dst_releases), 2)

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

    def test_cli_rejects_legacy_positional_project_argument(self):
        result = subprocess.run([sys.executable, '-B', str(ASSET), 'legacy-project'],
                                cwd=self.project, env={'HOME': str(self.root / 'home')},
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('不接受命令行参数', result.stderr)
        self.assertNotIn('legacy-project', result.stderr)

    def test_launch_from_other_cwd_without_credentials_stops_before_network(self):
        env = {key: value for key, value in os.environ.items() if key not in TOKENS}
        env['HOME'] = str(self.root / 'home')
        env.update(CONTROL_REF='refs/heads/main', CI_RUN_ID='42', CI_RUN_ATTEMPT='1',
                   RUNNER_TEMP=str(self.root), CI_AUTH_DIR=str(self.root / 'alphapulse-sync-42-1'))
        result = subprocess.run([sys.executable, '-B', str(ASSET)], cwd=self.project, env=env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('FORGEJO_TOKEN', result.stderr)
        self.assertNotIn('.config/forgejo/token', result.stderr)
        self.assertNotIn('root', result.stderr)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = (ASSET.parents[1] / '.forgejo/workflows/sync-github.yml').read_text()

    def test_manual_trusted_main_shared_concurrency_and_secret_scope(self):
        sections = dict(re.findall(r'(?ms)^(\w+):\n(.*?)(?=^\S|\Z)', self.workflow))
        self.assertEqual(sections['on'], '  workflow_dispatch:\n\n')
        self.assertEqual(sections['concurrency'],
                         '  group: alphapulse-delivery\n  cancel-in-progress: false\n\n')
        self.assertEqual(re.findall(r'(?m)^  (\w+):$', sections['jobs']), ['sync'])
        self.assertIn("    if: ${{ forgejo.ref == 'refs/heads/main' }}\n", self.workflow)
        self.assertIn('    runs-on: docker\n    container:\n      image: node:24-bookworm\n', self.workflow)
        self.assertIn('uses: https://github.com/actions/checkout@11d5960a326750d5838078e36cf38b85af677262',
                      self.workflow)
        self.assertIn('          ref: ${{ forgejo.sha }}\n          persist-credentials: false\n', self.workflow)
        self.assertIn('python3 git skopeo ca-certificates', self.workflow)
        self.assertIn("['USE_LFS']", self.workflow)
        self.assertIn('if [ "$use_lfs" = true ]', self.workflow)
        self.assertIn('git-lfs', self.workflow)
        mappings = dict(re.findall(r'(?m)^          (\w+): \$\{\{ secrets\.(\w+) \}\}$', self.workflow))
        self.assertEqual(mappings, {'FORGEJO_TOKEN': 'RELEASE_TOKEN', 'GITHUB_TOKEN': 'MIRROR_TOKEN'})
        steps = self.workflow.split('      - name: ')
        self.assertEqual(len(steps), 5)
        sync = steps[3]
        self.assertTrue(sync.startswith('Sync verified originals to GitHub and GHCR\n        env:\n'))
        self.assertEqual(sync.count('secrets.'), 2)
        for step in steps[:3] + steps[4:]:
            self.assertNotIn('secrets.', step)
        self.assertIn('        run: python3 scripts/sync-github.py\n', sync)
        self.assertIn("      CONTROL_REF: ${{ forgejo.ref }}\n"
                      "      CI_RUN_ID: ${{ forgejo.run_id }}\n"
                      "      CI_RUN_ATTEMPT: ${{ forgejo.run_attempt }}\n",
                      self.workflow)
        # Forgejo Runner forbids runner context in job env; it is available in step env.
        job_env = self.workflow.split('    env:\n', 1)[1].split('    steps:', 1)[0]
        self.assertNotIn('runner.', job_env)
        auth_line = '          CI_AUTH_DIR: ${{ runner.temp }}/alphapulse-sync-${{ forgejo.run_id }}-${{ forgejo.run_attempt }}\n'
        self.assertIn(auth_line, sync)
        self.assertIn('        env:\n'+auth_line, steps[4])
        self.assertIn('        if: ${{ always() }}\n', steps[4])
        self.assertFalse((ASSET.parents[1] / 'sysnc-github.sh').exists())

    def test_always_cleanup_removes_only_exact_run_directory(self):
        code = textwrap.dedent(self.workflow.split("<<'PY'\n", 1)[1]).rsplit('\nPY', 1)[0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            owned = root / 'alphapulse-sync-42-2'
            other = root / 'alphapulse-sync-42-1'
            other.mkdir()
            (other / 'keep').touch()
            environment = {'CI_RUN_ID': '42', 'CI_RUN_ATTEMPT': '2', 'RUNNER_TEMP': temporary,
                           'CI_AUTH_DIR': str(owned)}
            owned.mkdir()
            (owned / 'gitconfig').touch()
            with patch.dict(os.environ, environment, clear=True):
                exec(compile(code, '<workflow cleanup>', 'exec'), {})
                self.assertFalse(owned.exists())
                exec(compile(code, '<workflow cleanup>', 'exec'), {})
            for path in (other, root, other / owned.name):
                with self.subTest(path=path), patch.dict(os.environ, {**environment, 'CI_AUTH_DIR': str(path)}, clear=True):
                    with self.assertRaisesRegex(RuntimeError, 'outside this run/attempt'):
                        exec(compile(code, '<workflow cleanup>', 'exec'), {})
            owned.symlink_to(other, target_is_directory=True)
            with patch.dict(os.environ, environment, clear=True), self.assertRaises(RuntimeError):
                exec(compile(code, '<workflow cleanup>', 'exec'), {})
            self.assertTrue((other / 'keep').exists())
