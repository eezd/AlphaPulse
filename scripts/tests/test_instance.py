"""Exercise installed entrypoints with synthetic receipts and a fake Docker CLI."""
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deploy

SPEC = importlib.util.spec_from_file_location('instance', deploy.ROOT / 'deploy/instance.py')
instance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(instance)

FAKE_DOCKER = '''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
a=sys.argv[1:]
root=Path(os.environ['ENTRY_FIXTURE'])
fixture=json.loads((root/'fixture.json').read_text())
with (root/'calls.jsonl').open('a') as output:
 output.write(json.dumps({'args':a,'env':{k:v for k,v in os.environ.items() if k.startswith(('COMPOSE_','ALPHAPULSE_'))}})+'\\n')
if 'config' in a:
 if fixture.get('config_failure'):sys.exit(11)
 if '--format' in a:print(json.dumps(fixture['config']))
elif a[:2]==['image','inspect']:
 if fixture.get('missing_image')==a[-1]:sys.exit(12)
 print(json.dumps([fixture['images'][a[-1]]]))
elif 'up' in a:
 if fixture.get('up_failure'):sys.exit(13)
elif 'stop' in a:
 if fixture.get('stop_failure'):sys.exit(14)
elif 'ps' in a:
 print('\\n'.join(c['service'] for c in fixture['containers']))
elif a[0]=='inspect':
 for container in fixture['containers']:print(json.dumps(container))
else:sys.exit(99)
'''


class EntrypointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / 'AlphaPulse'
        self.directory.mkdir()
        deploy.install_entrypoints(self.directory)
        self.receipt = {'verified': True, 'commit': 'a' * 40, 'version': '20261101-aaaaaaaa', 'images': {
            part: {'digest': repository + '@sha256:' + digit * 64}
            for (part, repository), digit in zip(deploy.IMAGES.items(), ('1', '2'), strict=True)}}
        self.write_receipt()
        (self.directory / 'compose.yml').write_text('name: alphapulse-dev\nservices: {}\n')
        (self.directory / 'images.env').write_text('SYNTHETIC=only\n')
        self.expected = {name: instance.SEARCH_IMAGE if name == 'searxng' else deploy.IMAGES['web' if name == 'web' else 'backend'] + ':' + self.receipt['version'] for name in instance.SERVICES}
        images = {image: {'Id': 'local-' + str(i), 'Architecture': 'amd64', 'Os': 'linux', 'RepoDigests': [self.receipt['images']['web' if image.startswith(deploy.IMAGES['web'] + ':') else 'backend']['digest']] if image != instance.SEARCH_IMAGE else ['searxng/searxng@' + instance.SEARCH_DIGEST], 'Config': {'Labels': {
            'org.opencontainers.image.revision': self.receipt['commit'],
            'org.opencontainers.image.version': self.receipt['version']}}} for i, image in enumerate(set(self.expected.values()))}
        self.fixture = {'config': {'name': 'alphapulse-dev', 'services': {s: {'image': image} for s, image in self.expected.items()}},
                        'images': images, 'containers': [{'service': s, 'image': images[image]['Id'],
                            'state': {'Running': True, 'Paused': False, 'Health': {'Status': 'healthy'}}} for s, image in self.expected.items()]}
        binary = self.root / 'bin'
        binary.mkdir()
        (binary / 'docker').write_text(FAKE_DOCKER)
        (binary / 'docker').chmod(0o755)
        self.env = {'PATH': str(binary) + os.pathsep + os.environ['PATH'], 'ENTRY_FIXTURE': str(self.root)}

    def write_receipt(self):
        (self.directory / 'deployment.json').write_text(json.dumps(self.receipt))

    def invoke(self, action='start', code=0, extra_env=None, args=()):
        (self.root / 'fixture.json').write_text(json.dumps(self.fixture))
        (self.root / 'calls.jsonl').unlink(missing_ok=True)
        result = subprocess.run(['bash', str(self.directory / (action + '.sh')), *args], cwd='/',
                                env={**self.env, **(extra_env or {})}, text=True, capture_output=True)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        log = self.root / 'calls.jsonl'
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        if code:
            self.assertNotIn('实例已健康启动', result.stdout)
            self.assertNotIn('实例已停止', result.stdout)
        return calls

    def test_start_ignores_caller_overrides_and_checks_health(self):
        calls = self.invoke(extra_env={'COMPOSE_PROJECT_NAME': 'other', 'COMPOSE_PROFILES': 'other',
                                     'ALPHAPULSE_BACKEND_IMAGE': 'unaccepted', 'ALPHAPULSE_WEB_IMAGE': 'unaccepted'})
        self.assertTrue(all(not c['env'] for c in calls))
        up = next(c['args'] for c in calls if 'up' in c['args'])
        self.assertIn('--no-build', up)
        self.assertEqual(up[up.index('--pull') + 1], 'never')
        self.assertIn('--wait', up)
        self.assertEqual(up[up.index('--project-name') + 1], 'alphapulse-dev')
        self.assertEqual(up[up.index('--env-file') + 1], str(self.directory / 'images.env'))
        self.assertEqual(calls[-1]['args'][0], 'inspect')

    def test_replaced_search_tag_never_starts_any_service(self):
        self.fixture['images'][instance.SEARCH_IMAGE]['RepoDigests'] = []
        calls = self.invoke(code=1)
        self.assertFalse(any('up' in c['args'] for c in calls))

    def test_missing_or_mismatched_images_never_start_any_service(self):
        backend = deploy.IMAGES['backend'] + ':' + self.receipt['version']
        for case in ('missing', 'labels', 'selection', 'digest'):
            with self.subTest(case=case):
                if case == 'missing': self.fixture['missing_image'] = backend
                if case == 'labels': self.fixture['images'][backend]['Config']['Labels']['org.opencontainers.image.revision'] = 'b' * 40
                if case == 'selection': self.fixture['config']['services']['api']['image'] = 'unaccepted'
                if case == 'digest': self.fixture['images'][backend]['RepoDigests'] = []
                calls = self.invoke(code=1)
                self.assertFalse(any('up' in c['args'] for c in calls))
                self.fixture['config']['services']['api']['image'] = backend
                self.fixture.pop('missing_image', None)
                self.fixture['images'][backend]['RepoDigests'] = [self.receipt['images']['backend']['digest']]
                self.fixture['images'][backend]['Config']['Labels']['org.opencontainers.image.revision'] = 'a' * 40

    def test_receipt_must_be_verified_before_docker(self):
        self.receipt['verified'] = False
        self.write_receipt()
        self.assertEqual(self.invoke(code=1), [])

    def test_unhealthy_exited_missing_or_wrong_running_image_is_not_success(self):
        for case in ('unhealthy', 'exited', 'missing', 'image'):
            with self.subTest(case=case):
                original = json.loads(json.dumps(self.fixture['containers']))
                if case == 'unhealthy': self.fixture['containers'][0]['state']['Health']['Status'] = 'unhealthy'
                if case == 'exited': self.fixture['containers'][0]['state']['Running'] = False
                if case == 'missing': self.fixture['containers'].pop()
                if case == 'image': self.fixture['containers'][0]['image'] = 'unexpected'
                self.invoke(code=1)
                self.fixture['containers'] = original

    def test_pending_and_shared_lock(self):
        pending = self.directory / 'deployment.pending.json'
        pending.write_text('{"operation":"remove-cls","phase":"cleanup"}')
        self.assertEqual(self.invoke(code=1), [])
        calls = self.invoke('stop')
        self.assertEqual(calls[-1]['args'][-1], 'stop')
        pending.unlink()
        with (self.directory / '.deploy.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for action in ('start', 'stop'):
                self.assertEqual(self.invoke(action, code=1), [])

    def test_argument_config_up_stop_failures_propagate(self):
        for action in ('start', 'stop'):
            self.assertEqual(self.invoke(action, code=2, args=('extra',)), [])
            self.fixture['config_failure'] = True
            calls = self.invoke(action, code=1)
            self.assertFalse(any('up' in c['args'] or 'stop' in c['args'] for c in calls))
            self.fixture.pop('config_failure')
            self.fixture[action.replace('start', 'up') + '_failure'] = True
            self.invoke(action, code=1)
            self.fixture.pop(action.replace('start', 'up') + '_failure')

    def test_stop_retains_data_and_does_not_require_start_receipt(self):
        (self.directory / 'deployment.json').unlink()
        calls = self.invoke('stop')
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[-1]['args'][-1], 'stop')
        self.assertFalse(any(value in ('down', 'rm', '--volumes') for c in calls for value in c['args']))

    def test_duplicate_config_and_missing_env_fail_before_docker(self):
        duplicate = self.directory / 'compose.yaml'
        duplicate.write_text('name: other\n')
        self.assertEqual(self.invoke(code=1), [])
        duplicate.unlink()
        (self.directory / 'images.env').unlink()
        self.assertEqual(self.invoke('stop', code=1), [])


class InstallationTests(unittest.TestCase):
    def test_install_update_missing_entry_and_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            deploy.install_entrypoints(directory)
            for name in ('start.sh', 'stop.sh'):
                self.assertEqual((directory / name).stat().st_mode & 0o777, 0o755)
            old = directory / 'instance.py'
            old.write_text('previous managed version')
            manifest = json.loads((directory / 'entrypoints.json').read_text())
            manifest['instance.py'] = hashlib.sha256(old.read_bytes()).hexdigest()
            (directory / 'entrypoints.json').write_text(json.dumps(manifest))
            (directory / 'stop.sh').unlink()
            deploy.install_entrypoints(directory)
            self.assertEqual(old.read_bytes(), (deploy.ROOT / 'deploy/instance.py').read_bytes())
            self.assertTrue((directory / 'stop.sh').is_file())

    def test_customizations_are_preserved_before_any_entry_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            custom = directory / 'stop.sh'
            custom.write_text('custom stop command')
            with self.assertRaisesRegex(RuntimeError, '自定义脚本'):
                deploy.install_entrypoints(directory)
            self.assertEqual(custom.read_text(), 'custom stop command')
            self.assertFalse((directory / 'instance.py').exists())

    def test_installed_helper_is_self_contained_and_service_set_matches_cd(self):
        self.assertEqual(set(instance.SERVICES), set(deploy.SERVICES))
        self.assertEqual(instance.SEARCH_IMAGE, deploy.SEARXNG_IMAGE)
        self.assertEqual(instance.SEARCH_DIGEST, deploy.SEARXNG_DIGEST)
        subprocess.run(['bash', '-n', str(deploy.ROOT / 'deploy/start.sh'), str(deploy.ROOT / 'deploy/stop.sh')], check=True)

    def test_install_only_does_not_use_docker_or_change_deployment_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            receipt = directory / 'deployment.json'
            receipt.write_text('unchanged deployed receipt')
            with patch.object(deploy, 'INSTANCE', directory), patch.object(sys, 'argv', ['deploy.py', 'install-entrypoints']), \
                    patch.object(deploy, 'run') as run:
                deploy.main()
                run.assert_not_called()
            self.assertEqual(receipt.read_text(), 'unchanged deployed receipt')

    def test_first_deployment_installs_versioned_entries(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            build = {'version':'20261101-aaaaaaaa', 'commit': 'a' * 40, 'images': {part: {'digest': 'synthetic'} for part in deploy.IMAGES}}
            with patch.object(deploy, 'INSTANCE', directory), patch.object(deploy, 'branch_head', return_value=build['commit']), \
                    patch.object(deploy, 'validate_images'), patch.object(deploy, 'run', return_value=json.dumps([{'RepoDigests': ['searxng/searxng@' + deploy.SEARXNG_DIGEST]}])), \
                    patch.object(deploy, 'health'), patch.object(deploy.time, 'sleep'):
                deploy.deploy(build, directory)
            for name in ('start.sh', 'stop.sh', 'instance.py'):
                self.assertEqual((directory / name).read_bytes(), (deploy.ROOT / 'deploy' / name).read_bytes())
            self.assertFalse((directory / 'deployment.pending.json').exists())


if __name__ == '__main__':
    unittest.main()
