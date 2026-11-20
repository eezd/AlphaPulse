"""Temporary builds, immutable completion and proven same-SHA rebuilds."""
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import release

spec = importlib.util.spec_from_file_location('release_rebuild', Path(__file__).resolve().parents[1]/'manual-release.py')
manual = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manual)


class FakeAPI:
    token = 'synthetic'

    def __init__(self):
        self.releases = {}
        self.tags = {}
        self.files = {}
        self.images = {}
        self.runs = {}
        self.writes = []
        self.fail_upload = None
        self.fail_delete = False

    def release(self, version):
        return self.releases.get(version)

    def pages(self, path, **kwargs):
        return iter(list(self.releases.values()))

    def assets(self, record):
        return [{'name': name} for name in self.files[record['tag_name']]]

    def asset_bytes(self, record, name):
        return self.files[record['tag_name']][name]

    def upload(self, record, path):
        self.writes.append(('upload', path.name))
        if self.fail_upload == path.name:
            self.fail_upload = None
            raise RuntimeError('upload interrupted')
        self.files[record['tag_name']][path.name] = path.read_bytes()

    def repo(self, path, *, method='GET', data=None, missing=False):
        if path.startswith('/commits/'):
            sha = self.tags.get(path.removeprefix('/commits/'))
            return {'sha': sha} if sha else None
        if path.startswith('/actions/runs/'):
            return self.runs[int(path.rsplit('/', 1)[1])]
        if path == '/releases' and method == 'POST':
            self.writes.append(('create', data['tag_name']))
            record = {**data, 'id': 1, 'prerelease': False, 'published_at': '2026-11-19T00:00:00Z'}
            self.releases[data['tag_name']] = record
            self.files[data['tag_name']] = {}
            return record
        record = next(r for r in self.releases.values() if str(r['id']) == path.rsplit('/', 1)[1])
        self.writes.append((method, path))
        if method == 'DELETE':
            if self.fail_delete:
                raise RuntimeError('unknown deletion result')
            del self.releases[record['tag_name']]
            del self.files[record['tag_name']]
        elif method == 'PATCH':
            record.update(data)
        else:
            raise AssertionError((path, method))
        return record

    def request(self, path, *, method):
        self.writes.append((method, path))
        assert path.startswith('/packages/eezd/container/alphapulse/')
        del self.images[path.rsplit('/', 1)[1]]


class RebuildTests(unittest.TestCase):
    version = '20261109-aaaaaaaa'
    commit = 'a'*40
    value = 'sha256:'+'1'*64

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.api = FakeAPI()
        self.builds = 0
        self.fail_image_check = False
        self.run_id = '101'
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'CI_RUN_ID': self.run_id, 'REQUEST_ID': 'request-101', 'DOCKER_CONFIG': '/synthetic'}).start()
        patch.object(manual, 'registry_login').start()
        patch.object(manual, 'git_env', return_value={}).start()
        patch.object(manual, 'registry_digest', side_effect=lambda image, tag: self.api.images.get(tag)).start()
        patch.object(manual, 'image_digest', side_effect=self.inspect).start()
        patch.object(manual, 'verify_anonymous').start()
        patch.object(manual, 'release_notes', return_value='init').start()
        patch.object(manual, 'prepare_artifacts', side_effect=self.prepare).start()
        patch.object(manual, 'run', side_effect=self.command).start()

    def inspect(self, reference):
        if self.fail_image_check:
            self.fail_image_check = False
            raise RuntimeError('image verification interrupted')
        return self.api.images[reference.rsplit(':', 1)[1]]

    def command(self, *args, **kwargs):
        if args[0] == 'skopeo':
            self.api.writes.append(('image', self.version))
            self.api.images[self.version] = self.value
        elif args[:2] == ('git', 'push'):
            self.api.writes.append(('tag', self.version))
            self.api.tags[self.version] = self.commit
        return ''

    def prepare(self, cache, record):
        self.builds += 1
        packages = []
        for platform in release.PLATFORMS:
            path = cache/release.archive_name(self.version, platform)
            path.write_bytes(f'build-{self.builds}-{platform}'.encode())
            packages.append({'name': path.name, 'sha256': release.digest(path)})
        (cache/'SHA256SUMS').write_text(''.join(f'{p["sha256"]}  {p["name"]}\n' for p in packages))
        (cache/'candidate.oci').write_bytes(b'local-only')
        release.write_json(cache/'build.json', {**record, 'schema': 4, 'release_run_id': int(os.environ['CI_RUN_ID']),
                           'release_request_id': os.environ['REQUEST_ID'], 'packages': packages,
                           'image': {'ref': release.FORGEJO_IMAGE+':'+self.version, 'digest': self.value}})
        self.api.runs[int(os.environ['CI_RUN_ID'])] = {'id': int(os.environ['CI_RUN_ID']), 'status': 'failure',
            'event': '', 'trigger_event': 'workflow_dispatch', 'workflow_id': 'release.yml', 'event_payload': json.dumps({'inputs': {
                'source_branch': 'dev', 'source_sha': self.commit, 'request_id': os.environ['REQUEST_ID']}})}

    def publish(self):
        with tempfile.TemporaryDirectory(dir=self.temporary.name) as temporary:
            manual.publish(self.api, Path(temporary), 'dev', self.commit, self.version)

    def interrupt(self, name='SHA256SUMS'):
        self.api.fail_upload = name
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            self.publish()
        os.environ.update(CI_RUN_ID='102', REQUEST_ID='request-102')

    def test_first_release_builds_before_writes_and_completes_last(self):
        self.publish()
        self.assertEqual(self.builds, 1)
        self.assertEqual(self.api.writes[-1], ('upload', manual.COMPLETE))
        self.assertFalse(self.api.release(self.version)['draft'])
        self.assertEqual(set(self.api.files[self.version]), manual.published_files(self.version) | {manual.COMPLETE})

    def test_completed_release_is_reused_with_zero_writes_or_builds(self):
        self.publish()
        writes = list(self.api.writes)
        self.publish()
        self.assertEqual(self.builds, 1)
        self.assertEqual(self.api.writes, writes)

    def test_partial_upload_is_deleted_and_rebuilt_from_same_sha(self):
        self.interrupt()
        old_build = self.api.files[self.version]['build.json']
        self.publish()
        self.assertEqual(self.builds, 2)
        self.assertNotEqual(self.api.files[self.version]['build.json'], old_build)
        self.assertIn(('DELETE', '/releases/1'), self.api.writes)
        self.assertEqual(self.api.tags[self.version], self.commit)
        self.assertEqual(sum(action == 'tag' for action, _ in self.api.writes), 1)

    def test_incomplete_image_deletion_is_exact_and_other_version_untouched(self):
        self.fail_image_check = True
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            self.publish()
        os.environ.update(CI_RUN_ID='102', REQUEST_ID='request-102')
        self.api.images['another-version'] = 'sha256:'+'2'*64
        self.publish()
        self.assertIn(('DELETE', '/packages/eezd/container/alphapulse/'+self.version), self.api.writes)
        self.assertIn('another-version', self.api.images)

    def test_formal_release_without_marker_can_be_rebuilt_with_proven_identity(self):
        self.interrupt(manual.COMPLETE)
        self.assertFalse(self.api.release(self.version)['draft'])
        self.publish()
        self.assertEqual(self.builds, 2)

    def test_running_previous_writer_blocks_without_deletion_or_build(self):
        self.interrupt()
        self.api.runs[101]['status'] = 'running'
        writes = list(self.api.writes)
        with self.assertRaisesRegex(ValueError, 'not terminated'):
            self.publish()
        self.assertEqual(self.builds, 1)
        self.assertEqual(self.api.writes, writes)

    def test_previous_run_must_use_native_dispatch_trigger(self):
        self.interrupt()
        self.api.runs[101].update(event='workflow_dispatch', trigger_event='push')
        writes = list(self.api.writes)
        with self.assertRaisesRegex(ValueError, 'unproven'):
            self.publish()
        self.assertEqual(self.builds, 1)
        self.assertEqual(self.api.writes, writes)

    def test_current_run_cannot_delete_itself(self):
        self.interrupt()
        os.environ['CI_RUN_ID'] = '101'
        with self.assertRaisesRegex(ValueError, 'still active'):
            self.publish()

    def test_corrupt_completion_marker_is_not_treated_as_incomplete(self):
        self.publish()
        self.api.files[self.version][manual.COMPLETE] = b'{}'
        writes = list(self.api.writes)
        with self.assertRaisesRegex(ValueError, 'completion record differs'):
            self.publish()
        self.assertEqual(self.api.writes, writes)

    def test_marker_on_draft_is_not_deleted(self):
        self.publish()
        self.api.release(self.version)['draft'] = True
        with self.assertRaisesRegex(ValueError, 'non-formal'):
            self.publish()
        self.assertEqual(self.builds, 1)

    def test_missing_provenance_requires_manual_reconciliation(self):
        self.interrupt()
        del self.api.files[self.version]['build.json']
        with self.assertRaisesRegex(ValueError, 'manual reconciliation'):
            self.publish()
        self.assertEqual(self.builds, 1)

    def test_old_run_inputs_must_match_build_identity(self):
        self.interrupt()
        self.api.runs[101]['event_payload'] = json.dumps({'inputs': {'source_sha': 'b'*40}})
        with self.assertRaisesRegex(ValueError, 'inputs differ'):
            self.publish()

    def test_unproven_image_is_not_deleted(self):
        self.interrupt()
        self.api.images[self.version] = 'sha256:'+'9'*64
        with self.assertRaisesRegex(ValueError, 'image identity'):
            self.publish()

    def test_tag_collision_stops_before_build_or_remote_write(self):
        self.api.tags[self.version] = 'b'*40
        with self.assertRaisesRegex(ValueError, 'Tag collision'):
            self.publish()
        self.assertEqual(self.builds, 0)
        self.assertEqual(self.api.writes, [])

    def test_retry_preserves_original_notes_and_baseline(self):
        self.interrupt()
        self.api.release(self.version)['body'] = 'original notes'
        with patch.object(manual, 'release_notes', side_effect=AssertionError('must not regenerate notes')):
            self.publish()
        self.assertEqual(self.api.release(self.version)['body'], 'original notes')

    def test_retry_cannot_change_source_branch(self):
        self.interrupt()
        writes = list(self.api.writes)
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, 'incomplete build identity'):
                manual.publish(self.api, Path(temporary), 'main', self.commit, self.version)
        self.assertEqual(self.api.writes, writes)
        self.assertEqual(self.builds, 1)

    def test_failed_build_preserves_previous_failure_evidence(self):
        self.interrupt()
        before = copy.deepcopy(self.api.files)
        with patch.object(manual, 'prepare_artifacts', side_effect=RuntimeError('build failed')):
            with self.assertRaisesRegex(RuntimeError, 'build failed'):
                self.publish()
        self.assertEqual(self.api.files, before)
        self.assertFalse(any(method == 'DELETE' for method, _ in self.api.writes))

    def test_unknown_delete_result_does_not_start_new_uploads(self):
        self.interrupt()
        self.api.fail_delete = True
        writes = len(self.api.writes)
        with self.assertRaisesRegex(RuntimeError, 'unknown deletion'):
            self.publish()
        self.assertEqual(self.api.writes[writes:], [('DELETE', '/releases/1')])


class ExecuteTests(unittest.TestCase):
    def test_fixed_sha_fetch_uses_explicit_authenticated_forgejo_url(self):
        api = Mock(token='synthetic')
        api.request.return_value = {'login': 'eezd'}
        api.repo.return_value = {'commit': {'id': 'b'*40}}
        with patch.dict(os.environ, {'SOURCE_BRANCH': 'feature/demo', 'SOURCE_SHA': 'a'*40,
                                     'CONTROL_REF': 'refs/heads/main'}), \
             patch.object(manual, 'client', return_value=api), \
             patch.object(manual, 'run', side_effect=['', '0']) as run, \
             patch.object(manual, 'ancestor', return_value=True) as ancestor, \
             patch.object(manual, 'publish') as publish:
            manual.execute()
        self.assertEqual(api.repo.call_args.args, ('/branches/feature%2Fdemo',))
        self.assertEqual(run.call_args_list[0].args[:4], ('git', 'fetch', '--no-tags', release.FORGEJO_GIT_URL))
        self.assertIn('Authorization: Basic ', run.call_args_list[0].kwargs['env']['GIT_CONFIG_VALUE_1'])
        ancestor.assert_called_once_with('a'*40, 'b'*40)
        self.assertEqual(publish.call_args.args[2:], ('feature/demo', 'a'*40, '19700101-aaaaaaaa'))

    def test_untrusted_control_ref_stops_before_api_access(self):
        with patch.dict(os.environ, {'SOURCE_BRANCH': 'dev', 'SOURCE_SHA': 'a'*40, 'CONTROL_REF': 'refs/heads/dev'}), \
             patch.object(manual, 'client') as client:
            with self.assertRaisesRegex(ValueError, 'must run from main'):
                manual.execute()
        client.assert_not_called()


class OCIBuildTests(unittest.TestCase):
    builder = 'alphapulse-release-123'
    context = builder+'-context'

    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'CI_RUN_ID': '123', 'DOCKER_HOST': 'tcp://docker:2376',
                                'DOCKER_TLS_VERIFY': '1', 'DOCKER_CERT_PATH': '/synthetic/certificates'}).start()
        self.command = patch.object(manual, 'run', return_value='').start()
        self.cleanup = patch.object(manual.subprocess, 'run').start()
        self.operations = Mock()
        self.operations.attach_mock(self.command, 'checked')
        self.operations.attach_mock(self.cleanup, 'unchecked')

    def build(self):
        manual.build_oci(Path('/synthetic/image'), Path('/synthetic/candidate.oci'),
                         'a'*40, '20261109-aaaaaaaa', 'b'*64)

    def assert_cleanup(self):
        self.assertEqual(self.operations.mock_calls[-2:], [
            call.unchecked(['docker', 'buildx', 'rm', self.builder], check=False),
            call.unchecked(['docker', 'context', 'rm', self.context], check=False)])

    def test_tls_environment_is_captured_in_explicit_temporary_context(self):
        self.build()
        commands = [entry.args for entry in self.command.call_args_list]
        self.assertEqual(commands[0], ('docker', 'context', 'create', self.context))
        self.assertEqual(commands[1][:5], ('docker', 'buildx', 'create', '--name', self.builder))
        self.assertEqual(commands[1][-1], self.context)
        self.assertNotEqual(self.builder, self.context)
        self.assertEqual(commands[2][:5], ('docker', 'buildx', 'build', '--builder', self.builder))
        self.assertIn('--platform=linux/amd64,linux/arm64', commands[2])
        self.assert_cleanup()

    def test_builder_and_context_are_cleaned_after_builder_or_build_failure(self):
        for stage in (1, 2):
            with self.subTest(stage=stage):
                self.operations.reset_mock()
                failure = subprocess.CalledProcessError(1, ['docker', 'buildx'])
                self.command.side_effect = ['']*stage+[failure]
                with self.assertRaises(subprocess.CalledProcessError) as caught:
                    self.build()
                self.assertIs(caught.exception, failure)
                self.assert_cleanup()

    def test_context_creation_failure_does_not_remove_unowned_resources(self):
        self.command.side_effect = subprocess.CalledProcessError(1, ['docker', 'context', 'create'])
        with self.assertRaises(subprocess.CalledProcessError):
            self.build()
        self.command.assert_called_once_with('docker', 'context', 'create', self.context)
        self.cleanup.assert_not_called()


class ArtifactTests(unittest.TestCase):
    def test_actual_preparation_builds_without_credentials_or_checkpoints(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            downloads = root/'source/downloads'
            downloads.mkdir(parents=True)
            cache = root/'cache'
            cache.mkdir()
            record = {'source_sha': 'a'*40, 'version': '20261109-aaaaaaaa', 'source_branch': 'dev', 'previous_tag': None}
            files = {}
            for platform in release.PLATFORMS:
                path = downloads/release.archive_name(record['version'], platform)
                if platform.startswith('linux'):
                    with tarfile.open(path, 'w:gz') as archive:
                        content = b'synthetic package'
                        member = tarfile.TarInfo('target.json')
                        member.size = len(content)
                        archive.addfile(member, io.BytesIO(content))
                else:
                    path.write_bytes(b'synthetic cross-platform archive')
                files[path.name] = {'sha256': release.digest(path), 'size': path.stat().st_size}
            (downloads/'SHA256SUMS').write_text(''.join(f'{v["sha256"]}  {k}\n' for k, v in files.items()))
            release.write_json(downloads/'build.json', {'commit': record['source_sha'], 'version': record['version'],
                                                      'files': files, 'validation': {}})
            platforms = {'linux/amd64': 'sha256:'+'1'*64, 'linux/arm64': 'sha256:'+'2'*64}
            manifest = json.dumps({'manifests': [{'platform': {'os': 'linux', 'architecture': key.split('/')[1]},
                                                 'digest': value} for key, value in platforms.items()]}).encode()
            value = 'sha256:'+hashlib.sha256(manifest).hexdigest()

            def build_oci(image, destination, *args):
                outer = json.dumps({'manifests': [{'digest': value}]}).encode()
                with tarfile.open(destination, 'w') as archive:
                    for name, content in (('index.json', outer), ('blobs/sha256/'+value.removeprefix('sha256:'), manifest)):
                        member = tarfile.TarInfo(name)
                        member.size = len(content)
                        archive.addfile(member, io.BytesIO(content))

            def process(command, **kwargs):
                if len(command) > 1 and command[1].endswith('package.py'):
                    self.assertNotIn('FORGEJO_RELEASE_TOKEN', kwargs['env'])
                    self.assertNotIn('GITHUB_TOKEN', kwargs['env'])
                    self.assertNotIn('DOCKER_CONFIG', kwargs['env'])

            with patch.object(manual.Path, 'resolve', return_value=root/'source'), \
                 patch.object(manual.subprocess, 'run', side_effect=process), \
                 patch.object(manual, 'build_oci', side_effect=build_oci), patch.object(manual, 'run') as run, \
                 patch.dict(os.environ, {'CI_RUN_ID': '123', 'REQUEST_ID': 'fixture-request',
                                         'FORGEJO_RELEASE_TOKEN': 'fixture', 'GITHUB_TOKEN': 'fixture'}):
                manual.prepare_artifacts(cache, record)
            run.assert_called_once_with('docker', 'load', '--input', str(cache/'candidate.oci'))
            self.assertFalse((cache/'checkpoint.json').exists())
            build = json.loads((cache/'build.json').read_text())
            self.assertEqual(build['image'], {'digest': value, 'ref': release.FORGEJO_IMAGE+':'+record['version'], 'platforms': platforms})
            self.assertEqual(build['release_run_id'], 123)
            self.assertEqual({p['name']: p['sha256'] for p in build['packages']}, {n: f['sha256'] for n, f in files.items()})
