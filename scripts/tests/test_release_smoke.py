"""Release fixtures must work with a Docker daemon outside the checkout host."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
spec = importlib.util.spec_from_file_location('release_smoke', Path(__file__).resolve().parents[1]/'smoke-release.py')
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class ReleaseSmokeTests(unittest.TestCase):
    def exercise(self, *, fail_copy=False):
        calls = []

        def command(*args, **kwargs):
            calls.append(args)
            if args[:2] == ('docker', 'create'):
                return 'research-test-container'
            if args[:2] == ('docker', 'cp') and args[-1] == 'research-test-container:/research-fixtures' and fail_copy:
                raise RuntimeError('fixture copy failed')
            return ''

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'backend/tests/fixtures/research').mkdir(parents=True)
            (root/'scripts').mkdir()
            for name in ('remove_cls', 'research_sources', 'remove_trading', 'ai_recovery',
                         'remove_wscn_live', 'calendar', 'app', 'workspace'):
                (root/'scripts'/f'smoke_{name}.py').write_text('# synthetic input\n')
            with patch.dict(os.environ, {}, clear=True), patch.object(smoke, 'ROOT', root), \
                 patch.object(smoke, 'run', side_effect=command), \
                 patch.object(smoke.subprocess, 'run', return_value=SimpleNamespace(stdout='', stderr='')):
                if fail_copy:
                    with self.assertRaisesRegex(RuntimeError, 'fixture copy failed'):
                        smoke.smoke({'image': 'synthetic-image'}, 'synthetic-run')
                else:
                    smoke.smoke({'image': 'synthetic-image'}, 'synthetic-run')
        return calls

    def test_research_fixtures_are_copied_without_host_bind_mounts(self):
        calls = self.exercise()
        containers = [args for args in calls if args[:2] in (('docker', 'run'), ('docker', 'create'))]
        self.assertFalse(any('-v' in args or '--volume' in args or '--mount' in args for args in containers))
        create = next(i for i, args in enumerate(calls) if args[:2] == ('docker', 'create'))
        copy = next(i for i, args in enumerate(calls) if args[:2] == ('docker', 'cp') and args[-1] == 'research-test-container:/research-fixtures')
        start = calls.index(('docker', 'start', '-ai', 'research-test-container'))
        remove = calls.index(('docker', 'rm', '-fv', 'research-test-container'))
        self.assertLess(create, copy)
        self.assertLess(copy, start)
        self.assertLess(start, remove)

    def test_research_container_is_removed_when_fixture_copy_fails(self):
        calls = self.exercise(fail_copy=True)
        self.assertIn(('docker', 'rm', '-fv', 'research-test-container'), calls)
        self.assertNotIn(('docker', 'start', '-ai', 'research-test-container'), calls)
