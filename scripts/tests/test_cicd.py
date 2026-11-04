"""Verify publication gates, immutable identities and Git mirror reconciliation."""

from datetime import UTC, datetime
import os
import contextlib
import json
import fcntl
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ci
import deploy
import mirror


class BuildTests(unittest.TestCase):
    def test_search_health_checks_image_content_across_reference_styles(self):
        container = {'Name': '/search', 'Image': 'accepted-local-image', 'RestartCount': 0,
                     'Config': {'Image': 'historical@sha256:' + '1' * 64,
                                'Labels': {'com.docker.compose.service': 'searxng'}},
                     'State': {'Running': True, 'Paused': False}}
        with patch.object(deploy, 'inspect_services', return_value=[container]), \
                patch.object(deploy, 'run', side_effect=[json.dumps([{'Id': 'accepted-local-image'}]), 'commit', 'commit', '200']):
            deploy.health([], {'commit': 'commit'}, ('searxng',))
        with patch.object(deploy, 'inspect_services', return_value=[container]), \
                patch.object(deploy, 'run', return_value=json.dumps([{'Id': 'different-local-image'}])):
            with self.assertRaisesRegex(RuntimeError, 'Unexpected search image'):
                deploy.health([], {'commit': 'commit'}, ('searxng',))

    def test_version_is_utc_commit_identity(self):
        timestamp = datetime(2026, 10, 29, 23, 59, tzinfo=UTC).timestamp()
        self.assertEqual(ci.version('abcdef0123456789' * 2 + 'abcdef01', timestamp), '20261029-abcdef01')
        with self.assertRaises(RuntimeError):
            ci.version('abcdef0', timestamp)

    def test_published_outputs_use_tags_and_receipt_keeps_digests(self):
        build = {'verified': True, 'commit': 'a'*40, 'version': '20261101-aaaaaaaa',
                 'images': {part: {} for part in ci.IMAGES}}
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)
            (directory/'build.json').write_text(json.dumps(build))
            output=directory/'output'
            digests=[repository+'@sha256:'+'1'*64 for repository in ci.IMAGES.values()]
            with patch.object(ci,'ROOT',directory), patch.object(ci,'locked',return_value=contextlib.nullcontext()), \
                    patch.object(ci,'existing_image',side_effect=digests), \
                    patch.dict(os.environ,{'GITEA_EVENT_NAME':'push','GITEA_REF':'refs/heads/dev',
                                          'GITEA_SHA':build['commit'],'GITHUB_OUTPUT':str(output)}):
                ci.publish()
            published=json.loads((directory/'build.json').read_text())
            for (part,repository),digest in zip(ci.IMAGES.items(),digests):
                self.assertEqual(published['images'][part]['digest'],digest)
                self.assertIn(part+'='+repository+':'+build['version'],output.read_text())
            self.assertNotIn('@sha256:',output.read_text())

    def test_version_tag_is_checked_against_accepted_digest(self):
        build={'verified':True,'commit':'a'*40,'version':'20261101-aaaaaaaa',
               'images':{part:{'digest':repository+'@sha256:'+'1'*64} for part,repository in ci.IMAGES.items()}}
        def docker(*args):
            if args[:2]==('docker','pull'): return ''
            part=next(p for p,r in ci.IMAGES.items() if args[-1].startswith(r+':'))
            return json.dumps([{'RepoDigests':[build['images'][part]['digest']],
                'Architecture':'amd64','Os':'linux','Config':{'Labels':{
                    'org.opencontainers.image.revision':build['commit'],
                    'org.opencontainers.image.version':build['version']}}}])
        with patch.object(deploy,'run',side_effect=docker) as run:
            deploy.validate_images(build)
            self.assertTrue(all('@sha256:' not in call.args[-1] for call in run.call_args_list))
        with patch.object(deploy,'run',return_value=json.dumps([{'RepoDigests':[]}])):
            with self.assertRaisesRegex(RuntimeError,'differs from the accepted image'):
                deploy.validate_images(build)

    def test_existing_version_rejects_different_full_commit(self):
        commit = 'a' * 40
        manifest = {'config': {'digest': 'sha256:' + '0' * 64}}
        config = {'config': {'Labels': {'org.opencontainers.image.revision': 'a' * 39 + 'b',
                                       'org.opencontainers.image.version': '20261029-aaaaaaaa'}},
                  'architecture': 'amd64', 'os': 'linux'}
        with patch.object(ci, 'registry_json', side_effect=[(manifest, 'sha256:' + '1' * 64), (config, '')]):
            with self.assertRaisesRegex(RuntimeError, 'different commit'):
                ci.existing_image('backend', {'commit': commit, 'version': '20261029-aaaaaaaa'})

    def test_publication_rejects_pr_and_main_before_reading_artifacts(self):
        for event, ref in [('pull_request', 'refs/heads/dev'), ('push', 'refs/heads/main'), ('workflow_dispatch', 'refs/heads/dev')]:
            with self.subTest(event=event, ref=ref), patch.dict(os.environ, {'GITEA_EVENT_NAME': event, 'GITEA_REF': ref}):
                with self.assertRaisesRegex(RuntimeError, 'Only a dev push'):
                    ci.publish()

    def test_pending_deployment_blocks_all_updates(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / 'deployment.pending.json').write_text('{}')
            with patch.object(deploy, 'INSTANCE', directory), patch.object(deploy, 'branch_head') as head:
                with self.assertRaisesRegex(RuntimeError, 'pending deployment'):
                    deploy.deploy({}, directory)
                head.assert_not_called()

    def test_stale_deployment_does_not_pull_or_change_instance(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with patch.object(deploy, 'INSTANCE', directory), patch.object(deploy, 'branch_head', return_value='b' * 40), \
                    patch.object(deploy, 'validate_images') as validate:
                deploy.deploy({'commit': 'a' * 40}, directory)
                validate.assert_not_called()
            self.assertEqual({p.name for p in directory.iterdir()}, {'.deploy.lock'})

    def test_download_failure_is_not_a_missing_manifest(self):
        from urllib.error import HTTPError
        failure = HTTPError('https://git.134333.xyz/v2/x', 503, 'Unavailable', {}, None)
        with patch('urllib.request.urlopen', side_effect=failure):
            with self.assertRaises(HTTPError):
                ci.registry_json('x')

    def test_deployment_lock_is_shared_across_processes(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / '.deploy.lock'
            code = 'import fcntl,sys; f=open(sys.argv[1],"a"); fcntl.flock(f,fcntl.LOCK_EX); print("locked",flush=True); sys.stdin.read(1)'
            process = subprocess.Popen([sys.executable, '-c', code, str(lock_path)], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(process.stdout.readline().strip(), 'locked')
                with lock_path.open('a') as lock:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                process.communicate('x', timeout=5)
            with lock_path.open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


class NewsResetTests(unittest.TestCase):
    def test_preview_never_stops_worker_or_confirms_deletion(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            build = {'version':'20261101-aaaaaaaa', 'images': {'backend': {'digest': 'synthetic'}}}
            with patch.object(deploy, 'INSTANCE', directory), patch.object(deploy, 'validate_images'), patch.object(deploy, 'run', return_value='{}') as run:
                deploy.reset_news_ai(build)
                self.assertEqual(run.call_count, 2)
                self.assertTrue(run.call_args.kwargs['input'].startswith('CONFIRMED = False'))
                self.assertFalse(any('stop' in call.args for call in run.call_args_list))

    def test_reset_refuses_pending_deployment(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            ci.save(directory / 'deployment.pending.json', {})
            with patch.object(deploy, 'INSTANCE', directory), patch.object(deploy, 'validate_images'), patch.object(deploy, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'pending deployment'):
                    deploy.reset_news_ai({}, True)
                run.assert_not_called()

    def test_full_reset_requires_real_accepted_sample_before_docker(self):
        with patch.object(deploy,'validate_images') as images, patch.object(deploy,'run') as run:
            with self.assertRaisesRegex(RuntimeError,'验收尚未通过'):
                deploy.reset_news_derived({},True)
            images.assert_not_called()
            run.assert_not_called()

    def test_full_reset_preview_keeps_worker_and_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(deploy,'INSTANCE',Path(temporary)), patch.object(deploy,'validate_images'), patch.object(deploy,'run',return_value='{}') as run:
                deploy.reset_news_derived({'version':'synthetic'})
                self.assertEqual(run.call_count,2)
                self.assertIn('False,None,None',run.call_args.kwargs['input'])
                self.assertFalse(any('stop' in call.args for call in run.call_args_list))

    def test_full_reset_refuses_pending_deployment(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)
            ci.save(directory / 'deployment.pending.json',{})
            with patch.object(deploy,'INSTANCE',directory),patch.object(deploy,'validate_images'),patch.object(deploy,'run') as run:
                with self.assertRaisesRegex(RuntimeError,'pending deployment'):
                    deploy.reset_news_derived({})
                run.assert_not_called()


class RecoveryTests(unittest.TestCase):
    def test_empty_previous_running_list_keeps_all_services_stopped(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            ci.save(directory / 'deployment.pending.json', {})
            previous = {'version': '20261101-aaaaaaaa'}
            compose = 'services:\n  api:\n  news-labeling:\n'
            with patch.object(deploy, 'run', return_value='') as run, \
                    patch.object(deploy, 'validate_images'), patch.object(deploy, 'health') as health:
                deploy.restore(directory, {'previous': previous, 'previous_compose': compose, 'previous_running': []})
                self.assertFalse(any('up' in call.args for call in run.call_args_list))
                self.assertTrue(any('stop' in call.args for call in run.call_args_list))
                health.assert_not_called()
            self.assertFalse((directory / 'deployment.pending.json').exists())

    def test_rollback_removes_new_calendar_worker_and_checks_previous_services(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            ci.save(directory / 'deployment.pending.json', {})
            services = [name for name in deploy.SERVICES if name != 'jin10-calendar']
            previous = {'version':'20261101-aaaaaaaa', 'images': {part: {'digest': 'synthetic'} for part in ci.IMAGES}}
            compose = 'services:\n' + ''.join(f'  {name}:\n    image: synthetic\n' for name in services)
            with patch.object(deploy, 'run', return_value='') as run, patch.object(deploy, 'validate_search_image'), \
                    patch.object(deploy, 'validate_images'), patch.object(deploy, 'health') as health:
                deploy.restore(directory, {'previous': previous, 'previous_compose': compose, 'previous_running': services})
                update = next(call.args for call in run.call_args_list if 'up' in call.args)
                self.assertIn('--remove-orphans', update)
                self.assertNotIn('jin10-calendar', update)
                self.assertEqual(health.call_args.args[2], services)
            self.assertFalse((directory / 'deployment.pending.json').exists())

    def test_pending_write_failure_never_pauses_trader(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            previous = {'version':'20261101-aaaaaaaa', 'images': {part: {'digest': 'synthetic'} for part in ci.IMAGES}}
            ci.save(directory / 'deployment.json', previous)
            (directory / 'compose.yml').write_text('synthetic')
            running = [{'Config': {'Labels': {'com.docker.compose.service': 'trader'}}, 'State': {'Running': True}}]
            with patch.object(deploy, 'INSTANCE', directory), patch.object(deploy, 'branch_head', return_value='a' * 40), \
                    patch.object(deploy, 'validate_images'), patch.object(deploy, 'inspect_services', return_value=running), \
                    patch.object(deploy, 'run', return_value='{"busy":0}') as run, patch.object(deploy, 'validate_search_image'), \
                    patch.object(deploy, 'save', side_effect=OSError('disk failure')):
                with self.assertRaises(OSError):
                    deploy.deploy({**previous, 'commit': 'a' * 40}, directory)
                self.assertFalse(any(call.args[:2] == ('docker', 'pause') for call in run.call_args_list))

    def test_busy_recheck_recovers_without_stopping_services(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            previous = {'version':'20261101-aaaaaaaa', 'images': {part: {'digest': 'synthetic'} for part in ci.IMAGES}}
            ci.save(directory / 'deployment.json', previous)
            (directory / 'compose.yml').write_text('synthetic')
            running = [{'Config': {'Labels': {'com.docker.compose.service': 'trader'}}, 'State': {'Running': True}}]
            idle = iter(['{"busy":0}', '{"busy":1}'])
            def command(*args, **kwargs):
                if kwargs.get('input') == deploy.IDLE:
                    return next(idle)
                if args[:2] == ('docker', 'inspect'):
                    return '[{"State":{"Paused":true}}]'
                if args[:2] == ('docker', 'ps'):
                    return 'trader-id'
                if args[:3] == ('docker', 'image', 'inspect'):
                    return json.dumps([{'RepoDigests': ['searxng/searxng@' + deploy.SEARXNG_DIGEST]}])
                return '{}'
            with patch.object(deploy, 'INSTANCE', directory), patch.object(deploy, 'branch_head', return_value='a' * 40), \
                    patch.object(deploy, 'validate_images'), patch.object(deploy, 'inspect_services', return_value=running), \
                    patch.object(deploy, 'run', side_effect=command) as run, patch.object(deploy.subprocess, 'run'):
                with self.assertRaisesRegex(RuntimeError, 'Analysis became active'):
                    deploy.deploy({**previous, 'commit': 'a' * 40}, directory)
                self.assertFalse(any('stop' in call.args for call in run.call_args_list))
                self.assertTrue(any(call.args[:2] == ('docker', 'unpause') for call in run.call_args_list))
            self.assertFalse((directory / 'deployment.pending.json').exists())

    def test_preparation_recovery_unpauses_without_replacing_services(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            ci.save(directory / 'deployment.pending.json', {})
            with patch.object(deploy, 'run', side_effect=['trader-id', '[{"State":{"Paused":true}}]', '']) as run, \
                    patch.object(deploy, 'validate_images') as validate:
                deploy.restore(directory, {'previous': {}, 'phase': 'preparing'})
                run.assert_called_with('docker', 'unpause', 'trader-id')
                validate.assert_not_called()
            self.assertFalse((directory / 'deployment.pending.json').exists())

    def test_failed_unpause_preserves_pending(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            ci.save(directory / 'deployment.pending.json', {})
            with patch.object(deploy, 'run', side_effect=['trader-id', '[{"State":{"Paused":true}}]', RuntimeError('unpause failed')]):
                with self.assertRaisesRegex(RuntimeError, 'unpause failed'):
                    deploy.restore(directory, {'previous': {}, 'phase': 'preparing'})
            self.assertTrue((directory / 'deployment.pending.json').exists())

    def test_missing_or_wrong_github_identity_never_synchronizes(self):
        for token, identity in [('', None), ('synthetic', {'login': 'other'})]:
            with self.subTest(token=bool(token)), patch.dict(os.environ, {'ALL_GITHUB_TOKEN': token}), \
                    patch.object(sys, 'argv', ['mirror.py']), patch.object(mirror, 'request_json', return_value=identity), \
                    patch.object(mirror, 'synchronize') as sync:
                with self.assertRaises(RuntimeError):
                    mirror.main()
                sync.assert_not_called()

    def test_github_read_only_token_never_synchronizes(self):
        with patch.dict(os.environ, {'ALL_GITHUB_TOKEN': 'synthetic'}), patch.object(sys, 'argv', ['mirror.py']), \
                patch.object(mirror, 'request_json', side_effect=[{'login': 'eezd'}, {'permissions': {'push': False}}]), \
                patch.object(mirror, 'synchronize') as sync:
            with self.assertRaisesRegex(RuntimeError, 'write permission'):
                mirror.main()
            sync.assert_not_called()


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / 'source'
        self.target = self.directory / 'target'
        self.work = self.directory / 'work'
        self.work.mkdir()
        ci.run('git', 'init', '--bare', self.source)
        ci.run('git', 'init', '--bare', self.target)
        checkout = self.directory / 'checkout'
        ci.run('git', 'init', '--initial-branch=main', checkout)
        ci.run('git', '-C', checkout, 'config', 'user.name', 'CI')
        ci.run('git', '-C', checkout, 'config', 'user.email', 'ci@example.invalid')
        (checkout / 'fixture').write_text('synthetic\n')
        ci.run('git', '-C', checkout, 'add', 'fixture')
        ci.run('git', '-C', checkout, 'commit', '-m', 'Synthetic fixture')
        ci.run('git', '-C', checkout, 'push', str(self.source), 'main')
        self.sha = ci.run('git', '-C', checkout, 'rev-parse', 'HEAD')

    def test_all_business_refs_updates_deletions_and_annotated_tags(self):
        ci.run('git', '--git-dir', self.source, 'branch', 'dev', 'main')
        ci.run('git', '-c', 'user.name=CI', '-c', 'user.email=ci@example.invalid', '--git-dir', self.source,
               'tag', '-a', 'fixture-v1', '-m', 'Synthetic tag', 'main')
        mirror.synchronize(self.work, str(self.source), str(self.target))
        self.assertEqual(mirror.refs(self.source), mirror.refs(self.target))
        ci.run('git', '--git-dir', self.target, 'update-ref', 'refs/pull/1/head', self.sha)
        ci.run('git', '--git-dir', self.source, 'branch', '-D', 'dev')
        ci.run('git', '--git-dir', self.source, 'tag', '-d', 'fixture-v1')
        mirror.synchronize(self.work, str(self.source), str(self.target))
        self.assertEqual(mirror.refs(self.source), mirror.refs(self.target))
        self.assertEqual(ci.run('git', '--git-dir', self.target, 'rev-parse', 'refs/pull/1/head'), self.sha)
        self.assertTrue((self.work / 'last-sync.json').exists())

    def test_missing_source_main_never_deletes_target(self):
        mirror.synchronize(self.work, str(self.source), str(self.target))
        before = mirror.refs(self.target)
        ci.run('git', '--git-dir', self.source, 'branch', 'dev', 'main')
        ci.run('git', '--git-dir', self.source, 'branch', '-D', 'main')
        with self.assertRaisesRegex(RuntimeError, 'Source main is missing'):
            mirror.synchronize(self.work, str(self.source), str(self.target))
        self.assertEqual(before, mirror.refs(self.target))

    def test_failed_source_fetch_preserves_target(self):
        mirror.synchronize(self.work, str(self.source), str(self.target))
        before = mirror.refs(self.target)
        with self.assertRaises(RuntimeError):
            mirror.synchronize(self.work, str(self.directory / 'missing'), str(self.target))
        self.assertEqual(before, mirror.refs(self.target))


class TagRecoveryTests(unittest.TestCase):
    def test_recovery_converts_known_legacy_search_reference(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            ci.save(directory / 'deployment.pending.json', {})
            previous = {'version': '20261101-aaaaaaaa', 'images': {part: {'digest': 'synthetic'} for part in ci.IMAGES}}
            content = 'services:\n  searxng:\n    image: docker.io/searxng/searxng@' + deploy.SEARXNG_DIGEST + '\n'
            with patch.object(deploy, 'validate_images'), patch.object(deploy, 'validate_search_image'), \
                    patch.object(deploy, 'health'), patch.object(deploy, 'run', return_value=''):
                deploy.restore(directory, {'previous': previous, 'previous_compose': content, 'previous_running': ['searxng']})
            restored = (directory / 'compose.yml').read_text()
            self.assertIn(deploy.SEARXNG_IMAGE, restored)
            self.assertNotIn('@sha256:', restored)
            self.assertNotIn('@sha256:', (directory / 'images.env').read_text())

    def test_unknown_legacy_reference_cannot_restart_containers(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            ci.save(directory / 'deployment.pending.json', {})
            content = 'services:\n  searxng:\n    image: docker.io/searxng/searxng@sha256:' + '0' * 64 + '\n'
            with patch.object(deploy, 'validate_images'), patch.object(deploy, 'run', return_value='') as run:
                with self.assertRaisesRegex(RuntimeError, 'unknown digest'):
                    deploy.restore(directory, {'previous': {}, 'previous_compose': content, 'previous_running': ['searxng']})
                self.assertFalse(any('up' in c.args for c in run.call_args_list))
            self.assertTrue((directory / 'deployment.pending.json').exists())

    def test_search_tag_replacement_is_rejected(self):
        for digest in (deploy.SEARXNG_DIGEST, 'sha256:' + '0' * 64):
            with self.subTest(digest=digest), patch.object(deploy, 'run', return_value=json.dumps([{'RepoDigests': ['searxng/searxng@' + digest]}])) as run:
                if digest == deploy.SEARXNG_DIGEST:
                    deploy.validate_search_image()
                else:
                    with self.assertRaisesRegex(RuntimeError, 'Search version tag'):
                        deploy.validate_search_image()
                self.assertNotIn('@sha256:', run.call_args.args[-1])


if __name__ == '__main__':
    unittest.main()
