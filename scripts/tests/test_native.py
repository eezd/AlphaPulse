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
    def test_build_failure_does_not_change_running_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'config').mkdir()
            (root/'config/app.env').write_text('')
            with patch.object(native, 'RUNTIME', root), patch.object(native, 'run'), \
                 patch.object(native, 'build', side_effect=RuntimeError('build failed')), \
                 patch.object(native, 'activate') as activate:
                with self.assertRaisesRegex(RuntimeError, 'build failed'):
                    native.main()
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
            self.assertIn('User=alphapulse', content)
            self.assertIn('WorkingDirectory=', content)
            self.assertNotIn('ExecStartPre', content)
            self.assertIn('Restart=on-failure', content)
        self.assertIn('--port 21005', native.unit_text('api'))

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
