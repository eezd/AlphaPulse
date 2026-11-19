import fcntl
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import os
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import native


class NativeTests(unittest.TestCase):
    def check_update(self, active, *, activation_error=None, drain_error=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            previous = root/'old'; previous.mkdir()
            candidate = root/'new'; candidate.mkdir()
            (root/'current').symlink_to(previous)
            actions = []
            def maintenance(build, action):
                actions.append(action)
                if action == 'pause':
                    state = native.json.loads((root/'deployment.pending.json').read_text())
                    native.save(root/'deployment.pending.json', {**state, 'ai': {'enabled': True}})
                if action.startswith('drain') and drain_error:
                    raise drain_error
            with patch.object(native, 'RUNTIME', root), patch.object(native, 'active_roles', return_value=active), \
                 patch.object(native, 'maintenance', side_effect=maintenance), patch.object(native, 'healthy') as healthy, \
                 patch.object(native, 'activate', side_effect=activation_error) as activate:
                if activation_error or drain_error:
                    with self.assertRaisesRegex(RuntimeError, 'failure'):
                        native.update(candidate)
                    self.assertTrue((root/'deployment.pending.json').exists())
                else:
                    native.update(candidate)
                    self.assertFalse((root/'deployment.pending.json').exists())
                if activation_error or drain_error:
                    if active:
                        healthy.assert_called_once_with(previous, active)
                    else:
                        healthy.assert_not_called()
                return actions, activate.call_count

    def test_start_after_stop_skips_unavailable_api_and_activates(self):
        actions, activations = self.check_update(())
        self.assertEqual(actions, ['schema', 'schema', 'pause', 'assert-paused', 'restore'])
        self.assertEqual(activations, 1)

    def test_stopped_instance_activation_failure_restores_ai_without_api_health(self):
        actions, _ = self.check_update((), activation_error=RuntimeError('activation failure'))
        self.assertEqual(actions[-1], 'restore')

    def test_worker_only_instance_drains_without_chat_api(self):
        actions, _ = self.check_update(('news-risk',))
        self.assertIn('drain-without-api', actions)
        self.assertNotIn('drain', actions)

    def test_failed_drain_restores_settings_and_checks_only_original_active_roles(self):
        actions, activations = self.check_update(('api',), drain_error=RuntimeError('drain failure'))
        self.assertEqual(actions[-1], 'restore')
        self.assertEqual(activations, 0)

    def test_start_rejects_transitional_services_before_pausing_ai(self):
        with patch.object(native, 'run', return_value='deactivating'):
            with self.assertRaisesRegex(RuntimeError, 'existing service operation'):
                native.active_roles()

    def test_failed_activation_with_unexpected_running_services_does_not_resume_ai(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = root/'new'; candidate.mkdir()
            with patch.object(native, 'RUNTIME', root), \
                 patch.object(native, 'active_roles', side_effect=[(), ('api',)]), \
                 patch.object(native, 'maintenance') as maintenance, \
                 patch.object(native, 'activate', side_effect=RuntimeError('failed rollback')):
                with self.assertRaisesRegex(RuntimeError, 'left services running'):
                    native.update(candidate)
                self.assertNotIn('restore', [call.args[1] for call in maintenance.call_args_list])
                self.assertTrue((root/'deployment.pending.json').exists())

    def test_build_failure_does_not_change_running_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'config').mkdir()
            (root/'config/app.env').write_text('')
            with patch.object(native, 'RUNTIME', root), patch.object(native, 'run') as run, \
                 patch.object(native, 'build', side_effect=RuntimeError('build failed')), \
                 patch.object(native, 'activate') as activate:
                with self.assertRaisesRegex(RuntimeError, 'build failed'):
                    native.main()
                run.assert_not_called()
                activate.assert_not_called()

    def test_status_is_read_only_and_preserves_inactive_exit_code(self):
        with tempfile.TemporaryDirectory(prefix='alphapulse test ') as tmp:
            directory = Path(tmp)
            script = directory/'status.sh'
            script.write_bytes((Path(__file__).resolve().parents[2]/'status.sh').read_bytes())
            binary = directory/'systemctl'
            binary.write_text('#!/bin/sh\nexit 3\n')
            binary.chmod(0o755)
            result = subprocess.run(['bash', str(script)], cwd='/', capture_output=True,
                                    env={**os.environ, 'PATH': tmp+':'+os.environ['PATH']})
            self.assertEqual(result.returncode, 3)
            self.assertFalse((directory/'runtime').exists())

    def test_start_and_stop_respect_existing_deployment_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root/'runtime'
            (runtime/'config').mkdir(parents=True)
            (runtime/'config/app.env').touch()
            script = root/'stop.sh'
            script.write_bytes((native.ROOT/'stop.sh').read_bytes())
            with (runtime/'.deploy.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                with patch.object(native, 'RUNTIME', runtime), patch.object(native, 'build') as build:
                    with self.assertRaises(BlockingIOError):
                        native.main()
                    build.assert_not_called()
                systemctl = root/'systemctl'
                systemctl.write_text('#!/bin/sh\ntouch invoked-systemctl\n')
                systemctl.chmod(0o755)
                result = subprocess.run(['bash', str(script)], cwd='/', capture_output=True,
                                        env={**os.environ, 'PATH': tmp+':'+os.environ['PATH']})
                self.assertEqual(result.returncode, 1)
                self.assertIn('项目正在构建或启停'.encode(), result.stderr)
                self.assertFalse((root/'invoked-systemctl').exists())

    def test_pending_maintenance_blocks_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'config').mkdir()
            (root/'config/app.env').write_text('')
            (root/'deployment.pending.json').write_text('{}')
            with patch.object(native, 'RUNTIME', root), patch.object(native, 'run'), patch.object(native, 'build') as build:
                with self.assertRaisesRegex(RuntimeError, 'Pending maintenance'):
                    native.main()
                build.assert_not_called()

    def test_units_are_foreground_and_scoped(self):
        self.assertEqual(len(native.UNITS), 6)
        for role in native.ROLES:
            content = native.unit_text(role)
            self.assertNotIn('User=', content)
            self.assertNotIn('Group=', content)
            self.assertIn('Environment=HOME=%h', content)
            self.assertIn(f'Environment="PLAYWRIGHT_BROWSERS_PATH={native.RUNTIME}/ms-playwright"', content)
            self.assertIn(f'ExecStart="{native.ROOT}/start.sh" --systemd {role}', content)
            self.assertIn(f'ExecStop="{native.ROOT}/stop.sh" --systemd {role}', content)
            self.assertIn('Type=exec', content)
            self.assertNotIn('ExecStartPre', content)
            self.assertIn('Restart=on-failure', content)

    def test_systemd_entrypoints_run_foreground_without_service_management(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('start.sh', 'stop.sh'):
                script = root/name
                script.write_text((native.ROOT/name).read_text())
            backend = root/'runtime/current/backend'
            interpreter = backend/'.venv/bin/python'
            interpreter.parent.mkdir(parents=True)
            interpreter.write_text('#!/bin/sh\nprintf "%s\\n" "$PWD" "$@"\n')
            interpreter.chmod(0o755)
            # These modes need only the foreground interpreter, not build or systemctl commands.
            env = {'PATH': '/nonexistent'}
            dirname = root/'dirname'
            dirname.symlink_to('/usr/bin/dirname')
            for role, module in native.ROLES.items():
                with self.subTest(role=role):
                    result = subprocess.check_output(['/bin/bash', str(root/'start.sh'), '--systemd', role],
                                                     cwd='/', env={**env, 'PATH': tmp}, text=True).splitlines()
                    expected = ['uvicorn', module, '--host', '127.0.0.1', '--port', '21005',
                                '--proxy-headers', '--forwarded-allow-ips', '127.0.0.1'] if role == 'api' else [module]
                    self.assertEqual(result, [str(backend), '-m', *expected])
                    subprocess.run(['/bin/bash', str(root/'stop.sh'), '--systemd', role], cwd='/', env=env, check=True)
            self.assertFalse((root/'runtime/.deploy.lock').exists())

    def test_activation_failure_restores_previous_link_and_unit_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            previous = root/'old'; previous.mkdir()
            candidate = root/'new'; candidate.mkdir()
            (candidate/'build.json').write_text('{"source_sha":"new","version":"new"}')
            # Intercept system units so this test can never change the host.
            original = native.Path
            def path(value):
                return root/'units' if value == '/etc/systemd/system' else original(value)
            (root/'units').mkdir()
            (root/'current').symlink_to(previous)
            with patch.object(native, 'RUNTIME', root), patch.object(native, 'Path', side_effect=path), \
                 patch.object(native, 'run'), patch.object(native.subprocess, 'run') as process, \
                 patch.object(native, 'healthy', side_effect=RuntimeError('bad health')):
                process.return_value.returncode = 1
                with self.assertRaisesRegex(RuntimeError, 'bad health'):
                    native.activate(candidate)
            self.assertEqual((root/'current').resolve(), previous)
            self.assertFalse(list((root/'units').iterdir()))
