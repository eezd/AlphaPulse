"""Exercise retirement orchestration with synthetic configuration only."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import deploy


class RemoveClsTests(unittest.TestCase):
    def exercise(self, execute, failure=False):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            build = {'commit': 'a'*40, 'version': '20261104-aaaaaaaa'}
            (directory / 'deployment.json').write_text(json.dumps(build))
            calls = []

            def command(*args, **kwargs):
                calls.append(args)
                if kwargs.get('input') == deploy.IDLE:
                    return '{"busy":0}'
                if failure and '--execute' in args:
                    raise RuntimeError('cleanup failed')
                return '{}'

            with patch.object(deploy, 'INSTANCE', directory), patch.object(deploy, 'validate_images'), \
                    patch.object(deploy, 'branch_head', return_value=build['commit']), \
                    patch.object(deploy, 'run', side_effect=command), patch.object(deploy, 'deploy') as update:
                if failure:
                    with self.assertRaisesRegex(RuntimeError, 'cleanup failed'):
                        deploy.remove_cls(build, execute)
                    update.assert_not_called()
                else:
                    deploy.remove_cls(build, execute)
                    self.assertEqual(update.call_count, int(execute))
            return calls

    def test_preview_never_stops_migrates_or_deploys(self):
        calls = self.exercise(False)
        self.assertEqual(len(calls), 1)
        self.assertNotIn('--execute', calls[0])
        self.assertNotIn('alembic', calls[0])

    def test_execute_stops_before_cleanup_and_migration(self):
        calls = self.exercise(True)
        stop = next(i for i, args in enumerate(calls) if 'stop' in args)
        cleanup = next(i for i, args in enumerate(calls) if '--execute' in args)
        migration = next(i for i, args in enumerate(calls) if 'alembic' in args)
        self.assertLess(stop, cleanup)
        self.assertLess(cleanup, migration)

    def test_cleanup_failure_leaves_writers_stopped(self):
        calls = self.exercise(True, True)
        self.assertTrue(any('stop' in args for args in calls))
        self.assertFalse(any('up' in args or 'alembic' in args for args in calls))
