"""Temporary builds, immutable completion and proven same-SHA rebuilds."""
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
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
        if path.startswith('/git/commits/'):
            sha = self.tags.get(path.removeprefix('/git/commits/'))
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

    def inspect(self, reference, expected=None):
        if self.fail_image_check:
            self.fail_image_check = False
            raise RuntimeError('image verification interrupted')
        return self.api.images[reference.rsplit(':', 1)[1]]

    def command(self, *args, **kwargs):
        if args[0] == 'skopeo':
            tag = args[-1].rsplit(':', 1)[1]
            self.api.writes.append(('image', tag))
            if '--override-arch' in args:
                arch = args[args.index('--override-arch') + 1]
                self.assertEqual(args[:7], ('skopeo', '--override-os', 'linux', '--override-arch', arch, 'copy', '--preserve-digests'))
                self.api.images[tag] = self.platforms['linux/'+arch]
            else:
                self.assertEqual(args[1:4], ('copy', '--multi-arch=index-only', '--preserve-digests'))
                self.assertTrue(all(self.version+'-'+arch in self.api.images for arch in ('amd64', 'arm64')))
                self.api.images[tag] = self.value
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
        self.platforms = {'linux/amd64': 'sha256:'+'2'*64, 'linux/arm64': 'sha256:'+'3'*64}
        release.write_json(cache/'build.json', {**record, 'schema': 4, 'release_run_id': int(os.environ['CI_RUN_ID']),
                           'release_request_id': os.environ['REQUEST_ID'], 'packages': packages,
                           'image': {'ref': release.FORGEJO_IMAGE+':'+self.version, 'digest': self.value,
                                     'platforms': self.platforms,
                                     'architecture_tags': {platform: self.version+'-'+platform.split('/')[1]
                                                           for platform in self.platforms}}})
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
        self.api.runs[101]['event_payload'] = json.dumps({'ref': 'refs/heads/main', 'inputs': {'source_branch': 'dev', 'source_sha': 'b'*40}})
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

    def test_children_only_recovery_cleans_exact_owned_tags_and_preserves_git_tag(self):
        self.interrupt()
        self.api.images.update({self.version+'-'+arch: self.platforms['linux/'+arch]
                                for arch in ('amd64', 'arm64')})
        self.assertNotIn(self.version, self.api.images)
        writes = len(self.api.writes)
        self.publish()
        deletes = [path for method, path in self.api.writes[writes:] if method == 'DELETE']
        self.assertEqual(deletes, ['/packages/eezd/container/alphapulse/'+self.version+'-'+arch
                                  for arch in ('amd64', 'arm64')] + ['/releases/1'])
        self.assertEqual(self.api.tags[self.version], self.commit)
        self.assertEqual(sum(action == 'tag' for action, _ in self.api.writes), 1)

    def test_each_foreign_child_or_main_blocks_before_build_and_any_write(self):
        self.interrupt()
        writes = list(self.api.writes)
        for tag in (self.version, self.version+'-amd64', self.version+'-arm64'):
            with self.subTest(tag=tag):
                self.api.images = {tag: 'sha256:'+'9'*64}
                with self.assertRaisesRegex(ValueError, 'image identity'):
                    self.publish()
                self.assertEqual(self.api.writes, writes)
                self.assertEqual(self.builds, 1)

    def test_identity_change_after_replacement_build_never_deletes(self):
        self.interrupt()
        files, runs = copy.deepcopy(self.api.files), copy.deepcopy(self.api.runs)
        original = copy.deepcopy(self.api.releases)
        prepare = self.prepare
        for mutation in ('child-appears', 'child-changes', 'child-disappears', 'build', 'notes', 'draft', 'assets', 'git-tag'):
            with self.subTest(mutation=mutation):
                self.api.files, self.api.runs = copy.deepcopy(files), copy.deepcopy(runs)
                self.api.releases = copy.deepcopy(original)
                self.api.tags[self.version] = self.commit
                self.api.images = {self.version+'-amd64': self.platforms['linux/amd64']}
                def changed(cache, record, mutation=mutation):
                    prepare(cache, record)
                    if mutation == 'child-appears':
                        self.api.images[self.version+'-arm64'] = self.platforms['linux/arm64']
                    elif mutation == 'child-changes':
                        self.api.images[self.version+'-amd64'] = 'sha256:'+'9'*64
                    elif mutation == 'child-disappears':
                        self.api.images.clear()
                    elif mutation == 'build':
                        build = json.loads(self.api.files[self.version]['build.json'])
                        build['previous_tag'] = 'changed'
                        self.api.files[self.version]['build.json'] = json.dumps(build).encode()
                    elif mutation == 'assets':
                        self.api.files[self.version]['new.tar.gz'] = b'unknown'
                    elif mutation == 'git-tag':
                        self.api.tags[self.version] = 'b'*40
                    else:
                        self.api.release(self.version)[{'notes': 'body', 'draft': 'draft'}[mutation]] = 'changed'
                writes = list(self.api.writes)
                with patch.object(manual, 'prepare_artifacts', side_effect=changed), self.assertRaises(ValueError):
                    self.publish()
                self.assertEqual(self.api.writes, writes)

    def test_child_without_release_provenance_is_not_adopted_or_deleted(self):
        self.api.images[self.version+'-arm64'] = 'sha256:'+'3'*64
        with self.assertRaisesRegex(ValueError, 'Unproven existing image'):
            self.publish()
        self.assertEqual(self.api.writes, [])
        self.assertEqual(self.builds, 0)

    def test_failed_replacement_build_preserves_children_only_failure(self):
        self.interrupt()
        self.api.images[self.version+'-arm64'] = self.platforms['linux/arm64']
        before = copy.deepcopy((self.api.images, self.api.files, self.api.writes))
        with patch.object(manual, 'prepare_artifacts', side_effect=RuntimeError('build failed')):
            with self.assertRaisesRegex(RuntimeError, 'build failed'):
                self.publish()
        self.assertEqual((self.api.images, self.api.files, self.api.writes), before)

    def test_legacy_main_only_record_is_rebuilt_without_removing_other_versions(self):
        self.interrupt()
        build = json.loads(self.api.files[self.version]['build.json'])
        del build['image']['architecture_tags']
        self.api.files[self.version]['build.json'] = json.dumps(build).encode()
        self.api.images[self.version] = self.value
        self.api.images['other-version-arm64'] = 'sha256:'+'9'*64
        self.publish()
        self.assertIn('other-version-arm64', self.api.images)
        self.assertEqual({path for method, path in self.api.writes if method == 'DELETE'},
                         {'/packages/eezd/container/alphapulse/'+self.version, '/releases/1'})

    def test_legacy_completed_record_is_not_backfilled_or_rebuilt(self):
        self.publish()
        build = json.loads(self.api.files[self.version]['build.json'])
        del build['image']['architecture_tags']
        raw = json.dumps(build).encode()
        self.api.files[self.version]['build.json'] = raw
        self.api.files[self.version][manual.COMPLETE] = json.dumps({
            'version': self.version, 'source_sha': self.commit,
            'build_sha256': hashlib.sha256(raw).hexdigest()}).encode()
        self.api.images = {self.version: self.value}
        writes = list(self.api.writes)
        self.publish()
        self.assertEqual(self.api.writes, writes)
        self.assertEqual(self.builds, 1)

    def test_asset_order_and_download_counts_are_not_identity_changes(self):
        self.interrupt()
        original = self.api.assets
        def reordered(record):
            return [{**asset, 'download_count': self.builds} for asset in reversed(original(record))]
        with patch.object(self.api, 'assets', side_effect=reordered):
            self.publish()
        self.assertEqual(self.builds, 2)

    def test_last_moment_child_change_blocks_precise_delete(self):
        self.interrupt()
        tag = self.version+'-amd64'
        self.api.images[tag] = self.platforms['linux/amd64']
        reads = 0
        def changed(image, reference):
            nonlocal reads
            if reference == tag:
                reads += 1
                if reads == 3:
                    self.api.images[tag] = 'sha256:'+'9'*64
            return self.api.images.get(reference)
        writes = list(self.api.writes)
        with patch.object(manual, 'registry_digest', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'Image changed before deletion'):
                self.publish()
        self.assertEqual(self.api.writes, writes)

    def test_child_appearing_during_first_build_blocks_any_publication_write(self):
        prepare = self.prepare
        def appeared(cache, record):
            prepare(cache, record)
            self.api.images[self.version+'-amd64'] = self.platforms['linux/amd64']
        with patch.object(manual, 'prepare_artifacts', side_effect=appeared):
            with self.assertRaisesRegex(ValueError, 'Image appeared during build'):
                self.publish()
        self.assertEqual(self.api.writes, [])

    def test_unknown_delete_result_does_not_start_new_uploads(self):
        self.interrupt()
        self.api.fail_delete = True
        writes = len(self.api.writes)
        with self.assertRaisesRegex(RuntimeError, 'unknown deletion'):
            self.publish()
        self.assertEqual(self.api.writes[writes:], [('DELETE', '/releases/1')])


class ActionsProvenanceTests(RebuildTests):
    def setUp(self):
        super().setUp()
        patch.object(manual, 'ancestor', return_value=True).start()

    def prepare(self, cache, record):
        super().prepare(cache, record)
        build = json.loads((cache/'build.json').read_text())
        build.update(schema=5, control_sha='c'*40, source_branch_head=self.commit,
                     release_request_id='00000000-0000-4000-8000-000000000101',
                     release_inputs={'source_branch': 'dev', 'source_sha': ''})
        release.write_json(cache/'build.json', build)
        self.api.runs[int(os.environ['CI_RUN_ID'])].update(commit_sha='c'*40,
            event_payload=json.dumps({'ref': 'refs/heads/main', 'inputs': build['release_inputs']}))

    def test_new_provenance_mismatches_never_delete_or_build(self):
        self.interrupt()
        original_files, original_runs = copy.deepcopy(self.api.files), copy.deepcopy(self.api.runs)
        for mutation in ('control_sha', 'ref', 'inputs', 'schema'):
            with self.subTest(mutation=mutation):
                self.api.files, self.api.runs = copy.deepcopy(original_files), copy.deepcopy(original_runs)
                run = self.api.runs[101]
                event = json.loads(run['event_payload'])
                if mutation == 'control_sha':
                    run['commit_sha'] = 'd'*40
                elif mutation == 'ref':
                    event['ref'] = 'refs/heads/dev'
                elif mutation == 'inputs':
                    event['inputs']['source_sha'] = 'd'*40
                else:
                    build = json.loads(self.api.files[self.version]['build.json'])
                    build['schema'] = 99
                    self.api.files[self.version]['build.json'] = json.dumps(build).encode()
                run['event_payload'] = json.dumps(event)
                writes = list(self.api.writes)
                with self.assertRaises(ValueError):
                    self.publish()
                self.assertEqual(self.api.writes, writes)
                self.assertEqual(self.builds, 1)

    def test_old_branch_head_must_still_prove_ancestry(self):
        self.interrupt()
        with patch.object(manual, 'ancestor', return_value=False):
            with self.assertRaisesRegex(ValueError, 'branch history'):
                self.publish()
        self.assertFalse(any(action == 'DELETE' for action, _ in self.api.writes))


class ExecuteTests(unittest.TestCase):
    context = {'schema': 1, 'source_branch': 'feature/demo', 'source_sha': 'a'*40, 'source_branch_head': 'b'*40,
               'version': '19700101-aaaaaaaa', 'control_sha': 'c'*40, 'release_run_id': 123,
               'release_request_id': '00000000-0000-4000-8000-000000000123',
               'release_inputs': {'source_branch': 'feature/demo', 'source_sha': 'a'*40}}

    def test_fixed_sha_fetch_uses_explicit_authenticated_forgejo_url(self):
        api = Mock(token='synthetic')
        api.request.return_value = {'login': 'eezd'}
        api.repo.side_effect = AssertionError('Reachability must use fetched Git, not an earlier API snapshot')
        with patch.object(manual, 'release_context', return_value=self.context), \
             patch.object(manual, 'client', return_value=api), \
             patch.object(manual, 'run', side_effect=['a'*40, '', 'd'*40, '0']) as run, \
             patch.object(manual, 'ancestor', return_value=True) as ancestor, \
             patch.object(manual, 'publish') as publish:
            manual.execute()
        api.repo.assert_not_called()
        self.assertEqual(run.call_args_list[2].args, ('git', 'rev-parse', '--verify', 'refs/remotes/origin/feature/demo'))
        self.assertIn('+refs/heads/feature/demo:refs/remotes/origin/feature/demo', run.call_args_list[1].args)
        self.assertEqual(run.call_args_list[1].args[:4], ('git', 'fetch', '--no-tags', release.FORGEJO_GIT_URL))
        self.assertIn('Authorization: Basic ', run.call_args_list[1].kwargs['env']['GIT_CONFIG_VALUE_1'])
        self.assertEqual(ancestor.call_args_list, [call('a'*40, 'd'*40), call('a'*40, 'b'*40)])
        self.assertEqual(publish.call_args.args[2:], ('feature/demo', 'a'*40, '19700101-aaaaaaaa'))

    def test_rewritten_history_stops_before_publish(self):
        api = Mock(token='synthetic')
        api.request.return_value = {'login': 'eezd'}
        api.repo.return_value = {'commit': {'id': 'd'*40}}
        with patch.object(manual, 'release_context', return_value=self.context), \
             patch.object(manual, 'client', return_value=api), \
             patch.object(manual, 'run', return_value='a'*40), \
             patch.object(manual, 'ancestor', return_value=False), patch.object(manual, 'publish') as publish:
            with self.assertRaisesRegex(ValueError, 'no longer reachable'):
                manual.execute()
        publish.assert_not_called()

    def test_deleted_branch_cannot_reuse_stale_tracking_ref(self):
        api = Mock(token='synthetic')
        api.request.return_value = {'login': 'eezd'}
        with patch.object(manual, 'release_context', return_value=self.context), \
             patch.object(manual, 'client', return_value=api), \
             patch.object(manual, 'run', side_effect=['a'*40, subprocess.CalledProcessError(128, ['git', 'fetch'])]), \
             patch.object(manual, 'publish') as publish:
            with self.assertRaises(subprocess.CalledProcessError):
                manual.execute()
        publish.assert_not_called()

    def test_untrusted_control_ref_stops_before_api_access(self):
        with patch.dict(os.environ, {'CONTROL_REF': 'refs/heads/dev'}), patch.object(manual, 'client') as client:
            with self.assertRaisesRegex(ValueError, 'must run from main'):
                manual.execute()
        client.assert_not_called()

    def test_context_must_match_current_actions_identity_and_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'context.json'
            path.write_text(json.dumps(self.context))
            with patch.dict(os.environ, {'CONTROL_REF': 'refs/heads/main', 'CONTROL_SHA': 'c'*40,
                                         'CI_RUN_ID': '123', 'RELEASE_CONTEXT': str(path)}):
                self.assertEqual(manual.release_context(), self.context)
                for key, value in [('CI_RUN_ID', '124'), ('CONTROL_SHA', 'd'*40)]:
                    with patch.dict(os.environ, {key: value}), self.assertRaises(ValueError):
                        manual.release_context()
                bad = copy.deepcopy(self.context)
                bad['release_inputs']['source_sha'] = ''
                path.write_text(json.dumps(bad))
                with self.assertRaisesRegex(ValueError, 'inputs differ'):
                    manual.release_context()



class OCIBuildTests(unittest.TestCase):
    def test_oci_and_candidate_build_share_exact_attempt_builder(self):
        with patch.object(manual.candidate, 'ensure_builder', return_value='alphapulse-release-123-2'), \
             patch.dict(os.environ, {'CI_RUN_ID': '123', 'CI_RUN_ATTEMPT': '2'}), \
             patch.object(manual, 'run') as run:
            manual.build_oci(Path('/synthetic/image'), Path('/synthetic/candidate.oci'),
                             'a'*40, '20261109-aaaaaaaa', 'b'*64)
        command = run.call_args.args
        self.assertEqual(command[:5], ('docker', 'buildx', 'build', '--builder', 'alphapulse-release-123-2'))
        self.assertIn('--platform=linux/amd64,linux/arm64', command)
        self.assertIn('--output=type=oci,dest=/synthetic/candidate.oci,name=alphapulse-release-123-2-check:check', command)

    def test_builder_or_build_failure_propagates_for_outer_finally_cleanup(self):
        for at_builder in (True, False):
            failure = subprocess.CalledProcessError(1, ['docker', 'buildx'])
            with self.subTest(at_builder=at_builder), \
                 patch.dict(os.environ, {'CI_RUN_ID': '123', 'CI_RUN_ATTEMPT': '2'}), \
                 patch.object(manual.candidate, 'ensure_builder', side_effect=failure if at_builder else None,
                              return_value='owned'), \
                 patch.object(manual, 'run', side_effect=failure):
                with self.assertRaises(subprocess.CalledProcessError) as caught:
                    manual.build_oci(Path('/synthetic/image'), Path('/synthetic/candidate.oci'),
                                     'a'*40, '20261109-aaaaaaaa', 'b'*64)
                self.assertIs(caught.exception, failure)


class SyntheticRegistryTests(unittest.TestCase):
    """Run publisher and raw verifiers against an in-memory Registry and a real OCI archive."""

    def setUp(self):
        self.fixture = RebuildTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.manifests = {}
        self.inspections = []
        self.fail_arm64_once = False
        self.private_arm64 = False
        patch.object(manual, 'prepare_artifacts', side_effect=self.prepare).start()
        patch.object(manual, 'run', side_effect=self.command).start()
        patch.object(manual, 'image_digest', side_effect=release.image_digest).start()
        patch.object(manual, 'verify_anonymous', side_effect=release.verify_anonymous).start()
        patch.object(release, 'run', side_effect=self.inspect).start()
        authfile = Path(self.fixture.temporary.name)/'config.json'
        authfile.write_text(json.dumps({'auths': {'git.134333.xyz': {'auth': 'synthetic-test-account'}}}))
        patch.dict(os.environ, {'REGISTRY_AUTH_FILE': str(authfile), 'DOCKER_CONFIG': self.fixture.temporary.name}).start()

    def prepare(self, cache, record):
        self.fixture.prepare(cache, record)
        blobs, children = {}, []
        def blob(value):
            raw = json.dumps(value, separators=(',', ':')).encode()
            value = 'sha256:'+hashlib.sha256(raw).hexdigest()
            blobs['blobs/sha256/'+value.split(':')[1]] = raw
            return {'mediaType': json.loads(raw)['mediaType'], 'digest': value, 'size': len(raw)}
        for arch in ('amd64', 'arm64'):
            config = blob({'mediaType': 'application/vnd.oci.image.config.v1+json', 'architecture': arch,
                           'os': 'linux', 'rootfs': {'type': 'layers', 'diff_ids': []}})
            manifest = blob({'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                             'config': config, 'layers': []})
            children.append({**manifest, 'platform': {'os': 'linux', 'architecture': arch}})
        index = blob({'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.index.v1+json', 'manifests': children})
        self.fixture.value = index['digest']
        self.fixture.platforms = {'linux/'+item['platform']['architecture']: item['digest'] for item in children}
        files = {**blobs, 'oci-layout': b'{"imageLayoutVersion":"1.0.0"}',
                 'index.json': json.dumps({'schemaVersion': 2, 'manifests': [
                     {**index, 'annotations': {'org.opencontainers.image.ref.name': record['version']}}]}).encode()}
        with tarfile.open(cache/'candidate.oci', 'w') as archive:
            for name, raw in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(raw)
                archive.addfile(info, io.BytesIO(raw))
        build = json.loads((cache/'build.json').read_text())
        build['image'].update(digest=self.fixture.value, platforms=self.fixture.platforms)
        release.write_json(cache/'build.json', build)

    def command(self, *args, **kwargs):
        if args[0] != 'skopeo':
            return self.fixture.command(*args, **kwargs)
        source, tag = args[-2], args[-1].removeprefix('docker://')
        path, version = source.removeprefix('oci-archive:'), self.fixture.version
        with tarfile.open(path) as archive:
            descriptor = json.loads(archive.extractfile('index.json').read())['manifests'][0]
            self.assertEqual(descriptor['annotations']['org.opencontainers.image.ref.name'], version)
            raw = archive.extractfile('blobs/sha256/'+descriptor['digest'].split(':')[1]).read()
            children = json.loads(raw)['manifests']
            if '--override-arch' in args:
                self.assertEqual(args[:3], ('skopeo', '--override-os', 'linux'))
                arch = args[args.index('--override-arch')+1]
                self.assertEqual(args[5:7], ('copy', '--preserve-digests'))
                if self.fail_arm64_once and arch == 'arm64':
                    self.fail_arm64_once = False
                    raise RuntimeError('synthetic arm64 upload interrupted')
                descriptor = next(item for item in children if item['platform']['architecture'] == arch)
                raw = archive.extractfile('blobs/sha256/'+descriptor['digest'].split(':')[1]).read()
            else:
                self.assertEqual(args[1:4], ('copy', '--multi-arch=index-only', '--preserve-digests'))
                self.assertTrue(all(release.FORGEJO_IMAGE+'@'+item['digest'] in self.manifests for item in children))
            self.assertEqual('sha256:'+hashlib.sha256(raw).hexdigest(), descriptor['digest'])
            self.manifests[tag] = self.manifests[release.FORGEJO_IMAGE+'@'+descriptor['digest']] = raw
            self.fixture.api.images[tag.rsplit(':', 1)[1]] = descriptor['digest']
            self.fixture.api.writes.append(('image', tag.rsplit(':', 1)[1]))
        return ''

    def inspect(self, *args, **kwargs):
        self.assertEqual(args[:4], ('skopeo', 'inspect', '--raw', '--authfile'))
        auth = json.loads(Path(args[4]).read_text())
        if not auth['auths']:
            self.assertNotEqual(kwargs['env']['HOME'], os.environ['HOME'])
            self.assertEqual(kwargs['env']['REGISTRY_AUTH_FILE'], args[4])
        self.inspections.append((args[-1].removeprefix('docker://'), bool(auth['auths'])))
        if self.private_arm64 and not auth['auths'] and args[-1].endswith('-arm64'):
            raise subprocess.CalledProcessError(1, args, stderr=b'synthetic unauthorized')
        return self.manifests[args[-1].removeprefix('docker://')]

    def test_actual_oci_bytes_publish_children_first_without_digest_named_versions(self):
        self.fixture.publish()
        version = self.fixture.version
        self.assertEqual([tag for action, tag in self.fixture.api.writes if action == 'image'],
                         [version+'-amd64', version+'-arm64', version])
        self.assertEqual(set(self.fixture.api.images), {version, version+'-amd64', version+'-arm64'})
        for authenticated in (True, False):
            self.assertEqual(len([ref for ref, auth in self.inspections if auth == authenticated]), 10)
            self.assertTrue(all((release.FORGEJO_IMAGE+':'+version+suffix, authenticated) in self.inspections
                                for suffix in ('', '-amd64', '-arm64')))
        self.assertIn(manual.COMPLETE, self.fixture.api.files[version])

    def test_anonymous_readable_child_failure_keeps_release_draft_without_marker(self):
        self.private_arm64 = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.fixture.publish()
        self.assertTrue(self.fixture.api.release(self.fixture.version)['draft'])
        self.assertNotIn(manual.COMPLETE, self.fixture.api.files[self.fixture.version])
        self.assertFalse(any(method == 'PATCH' for method, _ in self.fixture.api.writes))

    def test_real_child_only_interruption_is_proven_then_precisely_rebuilt(self):
        self.fail_arm64_once = True
        with self.assertRaisesRegex(RuntimeError, 'arm64 upload interrupted'):
            self.fixture.publish()
        version = self.fixture.version
        self.assertEqual(set(self.fixture.api.images), {version+'-amd64'})
        os.environ.update(CI_RUN_ID='102', REQUEST_ID='request-102')
        self.fixture.publish()
        self.assertEqual([path for method, path in self.fixture.api.writes if method == 'DELETE'],
                         ['/packages/eezd/container/alphapulse/'+version+'-amd64', '/releases/1'])
        self.assertEqual(sum(action == 'tag' for action, _ in self.fixture.api.writes), 1)
        self.assertIn(manual.COMPLETE, self.fixture.api.files[version])


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

            def build_packages(source, control, commit, control_sha, version, destination):
                self.assertEqual((commit, control_sha, version), (record['source_sha'], 'c'*40, record['version']))
                shutil.copytree(downloads, destination, dirs_exist_ok=True)

            with patch.object(manual.Path, 'resolve', return_value=root/'source'), \
                 patch.object(manual.candidate, 'build_packages', side_effect=build_packages) as package, \
                 patch.object(manual.subprocess, 'run') as process, \
                 patch.object(manual, 'build_oci', side_effect=build_oci), patch.object(manual, 'run') as run, \
                 patch.object(manual, 'release_context', return_value={**record, 'release_run_id': 123,
                     'release_request_id': '00000000-0000-4000-8000-000000000123', 'control_sha': 'c'*40,
                     'source_branch_head': 'a'*40, 'release_inputs': {'source_branch': 'dev', 'source_sha': ''}}), \
                 patch.dict(os.environ, {'CI_RUN_ID': '123', 'CI_RUN_ATTEMPT': '2', 'FORGEJO_RELEASE_TOKEN': 'fixture', 'GITHUB_TOKEN': 'fixture'}):
                manual.prepare_artifacts(cache, record)
            package.assert_called_once()
            self.assertFalse(any('package.py' in str(call.args) for call in process.call_args_list))
            run.assert_called_once_with('docker', 'load', '--input', str(cache/'candidate.oci'))
            self.assertFalse((cache/'checkpoint.json').exists())
            build = json.loads((cache/'build.json').read_text())
            self.assertEqual(build['image'], {'digest': value, 'ref': release.FORGEJO_IMAGE+':'+record['version'],
                                              'platforms': platforms,
                                              'architecture_tags': {platform: record['version']+'-'+platform.split('/')[1]
                                                                    for platform in platforms}})
            self.assertEqual(build['schema'], 5)
            self.assertEqual(build['control_sha'], 'c'*40)
            self.assertEqual(build['release_inputs'], {'source_branch': 'dev', 'source_sha': ''})
            self.assertEqual(build['release_run_id'], 123)
            self.assertEqual({p['name']: p['sha256'] for p in build['packages']}, {n: f['sha256'] for n, f in files.items()})
