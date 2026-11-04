"""Exercise retirement orchestration with synthetic configuration only."""
import fcntl
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deploy


class RemoveClsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.build = {'commit': 'a' * 40, 'version': '20261104-aaaaaaaa'}
        self.previous = {'commit': 'b' * 40, 'version': '20261103-bbbbbbbb'}
        self.marker = self.directory / 'deployment.pending.json'
        deploy.save(self.directory / 'deployment.json', self.previous)
        (self.directory / 'compose.yml').write_text('services:\n  api:\n  trader:\n  news-labeling:\n')
        source = self.directory / 'source' / 'deploy'
        source.mkdir(parents=True)
        (source / 'docker-compose.yml').write_text('services:\n  api:\n  trader:\n')
        (source / 'searxng.yml').write_text('secret: @SEARXNG_SECRET@\n')
        self.calls = []
        self.running = True
        self.paused = False
        self.failure = lambda args, kwargs: None
        self.enterContext(patch.object(deploy, 'INSTANCE', self.directory))
        self.enterContext(patch.object(deploy, 'ROOT', source.parent))
        self.enterContext(patch.object(deploy, 'validate_images'))
        self.enterContext(patch.object(deploy, 'validate_search_image'))
        self.enterContext(patch.object(deploy, 'install_entrypoints'))
        self.lock = self.enterContext(patch.object(deploy, 'locked', wraps=deploy.locked))
        self.enterContext(patch.object(deploy.time, 'sleep'))
        self.head = self.enterContext(patch.object(deploy, 'branch_head', return_value=self.build['commit']))
        self.health = self.enterContext(patch.object(deploy, 'health'))
        self.enterContext(patch.object(deploy, 'inspect_services', side_effect=self.containers))
        self.enterContext(patch.object(deploy, 'run', side_effect=self.command))
        # Failure cleanup bypasses run(); it must also stay inside this fixture.
        self.enterContext(patch.object(deploy.subprocess, 'run', side_effect=lambda args, **kwargs: self.command(*args)))

    def containers(self, command):
        return [{'Config': {'Labels': {'com.docker.compose.service': name}},
                 'State': {'Running': self.running, 'Paused': self.paused if name == 'trader' else False}}
                for name in ('api', 'trader')]

    def assert_locked(self):
        with (self.directory / '.deploy.lock').open('a') as stream:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def command(self, *args, **kwargs):
        self.calls.append(args)
        if '--execute' in args or 'alembic' in args or 'up' in args:
            self.assert_locked()
            self.assertTrue(self.marker.exists())
        self.failure(args, kwargs)
        if kwargs.get('input') == deploy.IDLE:
            return '{"busy":0}'
        if args[:2] == ('docker', 'pause'):
            self.assertEqual(json.loads(self.marker.read_text())['phase'], 'preparing')
            self.paused = True
        if args[:2] == ('docker', 'ps'):
            return 'trader-id' if self.running else ''
        if args[:2] == ('docker', 'inspect'):
            return json.dumps([{'State': {'Paused': self.paused}}])
        if args[:2] == ('docker', 'unpause'):
            self.paused = False
        if 'stop' in args:
            self.running = self.paused = False
        if 'up' in args:
            self.running = True
        return '{}'

    def fail_command(self, target, message):
        def fail(args, kwargs):
            if target in args:
                raise RuntimeError(message)
        self.failure = fail

    def test_preview_never_stops_migrates_or_deploys(self):
        deploy.remove_cls(self.build)
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn('--execute', self.calls[0])
        self.assertNotIn('alembic', self.calls[0])
        self.assertFalse(self.marker.exists())
        self.health.assert_not_called()

    def test_execute_holds_lock_through_cleanup_migration_and_activation(self):
        deploy.remove_cls(self.build, True)
        stop = next(i for i, args in enumerate(self.calls) if 'stop' in args)
        cleanup = next(i for i, args in enumerate(self.calls) if '--execute' in args)
        migration = next(i for i, args in enumerate(self.calls) if 'alembic' in args)
        activation = next(i for i, args in enumerate(self.calls) if 'up' in args)
        self.assertLess(stop, cleanup)
        self.assertLess(cleanup, migration)
        self.assertLess(migration, activation)
        self.assertEqual(self.lock.call_count, 1)
        self.assertFalse(self.marker.exists())
        self.assertEqual(json.loads((self.directory / 'deployment.json').read_text()), self.build)
        self.health.assert_called_once()

    def test_cleanup_failure_blocks_update_recovery_and_keeps_writers_stopped(self):
        self.fail_command('--execute', 'cleanup failed')
        with self.assertRaisesRegex(RuntimeError, 'cleanup failed'):
            deploy.remove_cls(self.build, True)
        self.assertFalse(self.running)
        self.assertFalse(any('up' in args or 'alembic' in args for args in self.calls))
        pending = json.loads(self.marker.read_text())
        self.assertEqual(pending['phase'], 'cleanup')
        count = len(self.calls)
        with self.assertRaisesRegex(RuntimeError, 'pending deployment'):
            deploy.deploy(self.build, self.directory)
        with patch.object(sys, 'argv', ['deploy.py', 'rollback']):
            with self.assertRaisesRegex(RuntimeError, 'pending deployment'):
                deploy.main()
        with self.assertRaisesRegex(RuntimeError, 'CLS maintenance is incomplete'):
            deploy.restore(self.directory, pending)
        self.assertEqual(len(self.calls), count)
        self.assertTrue(self.marker.exists())

    def test_cleanup_failure_can_resume_under_the_same_lock(self):
        self.fail_command('--execute', 'cleanup failed')
        with self.assertRaises(RuntimeError):
            deploy.remove_cls(self.build, True)
        self.failure = lambda args, kwargs: None
        deploy.remove_cls(self.build, True)
        self.assertFalse(self.marker.exists())
        self.assertEqual(sum('--execute' in args for args in self.calls), 2)
        self.health.assert_called_once()

    def test_migration_retry_does_not_repeat_successful_cleanup(self):
        self.fail_command('alembic', 'migration failed')
        with self.assertRaisesRegex(RuntimeError, 'migration failed'):
            deploy.remove_cls(self.build, True)
        self.assertEqual(json.loads(self.marker.read_text())['phase'], 'migration')
        self.assertFalse(self.running)
        self.failure = lambda args, kwargs: None
        deploy.remove_cls(self.build, True)
        self.assertEqual(sum('--execute' in args for args in self.calls), 1)
        self.assertEqual(sum('alembic' in args for args in self.calls), 2)
        self.assertFalse(self.marker.exists())

    def test_failed_activation_stops_candidates_without_restoring_old_workers(self):
        self.health.side_effect = RuntimeError('health failed')
        with self.assertRaisesRegex(RuntimeError, 'health failed'):
            deploy.remove_cls(self.build, True)
        self.assertFalse(self.running)
        pending = json.loads(self.marker.read_text())
        self.assertEqual(pending['operation'], 'remove-cls')
        self.assertEqual(pending['phase'], 'updating')
        self.assertEqual(json.loads((self.directory / 'deployment.json').read_text()), self.previous)
        last_up = max(i for i, args in enumerate(self.calls) if 'up' in args)
        self.assertIn('stop', self.calls[-1])
        self.assertFalse(any('up' in args for args in self.calls[last_up + 1:]))
        self.health.side_effect = None
        deploy.remove_cls(self.build, True)
        self.assertEqual(sum('--execute' in args for args in self.calls), 1)
        self.assertEqual(sum('alembic' in args for args in self.calls), 1)
        self.assertFalse(self.marker.exists())

    def test_execute_resumes_with_all_writers_stopped(self):
        self.running = False
        deploy.remove_cls(self.build, True)
        self.assertFalse(any(args[:2] == ('docker', 'pause') for args in self.calls))
        self.assertTrue(any('--execute' in args for args in self.calls))
        self.assertFalse(self.marker.exists())

    def test_idle_recheck_exception_unpauses_without_stopping_services(self):
        checks = 0

        def fail(args, kwargs):
            nonlocal checks
            if kwargs.get('input') == deploy.IDLE:
                checks += 1
                if checks == 2:
                    raise RuntimeError('idle check failed')
        self.failure = fail
        with self.assertRaisesRegex(RuntimeError, 'idle check failed'):
            deploy.remove_cls(self.build, True)
        self.assertTrue(self.running)
        self.assertFalse(self.paused)
        self.assertFalse(self.marker.exists())
        self.assertFalse(any('stop' in args or '--execute' in args for args in self.calls))

    def test_failed_preparation_recovery_preserves_marker_for_explicit_recovery(self):
        def fail(args, kwargs):
            if kwargs.get('input') == deploy.IDLE and self.paused:
                raise RuntimeError('idle check failed')
            if args[:2] == ('docker', 'unpause'):
                raise RuntimeError('unpause failed')
        self.failure = fail
        with self.assertRaisesRegex(RuntimeError, 'unpause failed'):
            deploy.remove_cls(self.build, True)
        self.assertTrue(self.paused)
        pending = json.loads(self.marker.read_text())
        self.assertEqual(pending['phase'], 'preparing')
        self.failure = lambda args, kwargs: None
        with deploy.locked(self.directory / '.deploy.lock'):
            deploy.restore(self.directory, pending)
        self.assertFalse(self.marker.exists())
        self.assertFalse(self.paused)
        self.assertTrue(self.running)

    def test_failed_transition_write_unpauses_before_any_stop(self):
        save = deploy.save

        def fail(path, value):
            if value.get('phase') == 'stopping':
                raise OSError('disk failure')
            save(path, value)
        with patch.object(deploy, 'save', side_effect=fail):
            with self.assertRaisesRegex(OSError, 'disk failure'):
                deploy.remove_cls(self.build, True)
        self.assertFalse(self.paused)
        self.assertTrue(self.running)
        self.assertFalse(self.marker.exists())
        self.assertFalse(any('stop' in args for args in self.calls))

    def test_initial_marker_failure_never_pauses_or_stops(self):
        with patch.object(deploy, 'save', side_effect=OSError('disk failure')):
            with self.assertRaisesRegex(OSError, 'disk failure'):
                deploy.remove_cls(self.build, True)
        self.assertFalse(any('stop' in args or 'pause' in args for args in self.calls))

    def test_stale_maintenance_retains_marker_and_can_resume_with_new_accepted_build(self):
        # The branch advances after cleanup, immediately before candidate preparation.
        self.head.side_effect = [self.build['commit'], 'c' * 40]
        with self.assertRaisesRegex(RuntimeError, 'Maintenance build is stale'):
            deploy.remove_cls(self.build, True)
        self.assertFalse(self.running)
        self.assertFalse(any('up' in args for args in self.calls))
        self.assertEqual(json.loads(self.marker.read_text())['phase'], 'deploy-ready')
        newer = {'commit': 'c' * 40, 'version': '20261104-cccccccc'}
        self.head.side_effect = None
        self.head.return_value = newer['commit']
        deploy.remove_cls(newer, True)
        self.assertEqual(sum('--execute' in args for args in self.calls), 1)
        self.assertEqual(sum('alembic' in args for args in self.calls), 1)
        self.assertFalse(self.marker.exists())
        self.assertEqual(json.loads((self.directory / 'deployment.json').read_text()), newer)

    def test_branch_advance_after_candidate_validation_never_skips_successfully(self):
        self.head.side_effect = [self.build['commit'], self.build['commit'], 'c' * 40]
        with self.assertRaisesRegex(RuntimeError, 'Maintenance build is stale'):
            deploy.remove_cls(self.build, True)
        self.assertFalse(self.running)
        self.assertTrue(self.marker.exists())
        self.assertFalse(any('up' in args for args in self.calls))

    def test_other_pending_deployment_blocks_cleanup(self):
        deploy.save(self.marker, {'phase': 'updating'})
        with self.assertRaisesRegex(RuntimeError, 'Recover the pending deployment first'):
            deploy.remove_cls(self.build, True)
        self.assertEqual(self.calls, [])
