"""OCI latest is mutable; formal versions and Git tags remain immutable."""
import importlib.util
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
spec = importlib.util.spec_from_file_location('latest_release', Path(__file__).resolve().parents[1]/'manual-release.py')
manual = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manual)


class LatestTests(unittest.TestCase):
    def setUp(self):
        self.old = {'source_branch': 'main', 'source_sha': 'a', 'version': '20990101-ffffffff', 'image_digest': 'old'}
        self.new = {'source_branch': 'main', 'source_sha': 'b', 'version': '20000101-00000000',
                    'image_digest': 'new', 'image': {'digest': 'new', 'platforms': {}}}
        self.api = Mock()
        self.api.release.side_effect = lambda version: {'tag_name': version}
        self.current = None
        self.calls = []
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'DOCKER_CONFIG': '/synthetic'}).start()
        patch.object(manual, 'complete_history', return_value=[self.new, self.old]).start()
        patch.object(manual, 'ancestor', side_effect=lambda a, b: a == b or (a, b) in {('a', 'b'), ('a', 'head'), ('b', 'head')}).start()
        patch.object(manual, 'registry_digest', side_effect=lambda *args: self.current).start()
        patch.object(manual, 'image_digest', side_effect=lambda *args: self.current).start()
        self.verify = patch.object(manual, 'verify_completed_record').start()
        self.anonymous = patch.object(manual, 'verify_anonymous').start()
        patch.object(manual, 'run', side_effect=self.command).start()

    def command(self, *args, **kwargs):
        self.calls.append(args)
        if args[:2] == ('git', 'rev-parse'):
            return 'head'
        self.assertEqual(args[0], 'skopeo')
        self.assertEqual(args[1:4], ('copy', '--multi-arch=index-only', '--preserve-digests'))
        self.assertEqual(args[-2:], ('docker://'+manual.FORGEJO_IMAGE+'@new', 'docker://'+manual.FORGEJO_IMAGE+':latest'))
        self.current = 'new'

    def test_only_newest_completed_main_promoted_by_ancestry_not_version_name(self):
        for current in (None, 'old'):
            with self.subTest(current=current):
                self.current = current
                manual.promote_latest(self.api, 'main')
                self.assertEqual(self.current, 'new')
                self.verify.assert_called_with(self.api, {'tag_name': self.new['version']}, self.new)
                self.anonymous.assert_called_with(manual.FORGEJO_IMAGE+':latest', self.new['image'])
        self.api.repo.assert_not_called()
        self.api.upload.assert_not_called()

    def test_historical_rerun_never_rolls_back_and_existing_latest_is_reused(self):
        self.current = 'new'
        manual.promote_latest(self.api, 'main')
        self.assertEqual(self.current, 'new')
        self.assertEqual(self.calls, [('git', 'rev-parse', 'refs/remotes/origin/main')])

    def test_dev_release_does_not_read_or_write_latest(self):
        manual.promote_latest(self.api, 'dev')
        manual.complete_history.assert_not_called()
        self.assertEqual(self.calls, [])
        self.verify.assert_not_called()

    def test_main_reuse_of_dev_only_completed_version_skips_latest_without_failure(self):
        dev = {**self.new, 'source_branch': 'dev'}
        with patch.object(manual, 'complete_history', return_value=[dev]):
            manual.promote_latest(self.api, 'main')
        self.assertEqual(self.calls, [])
        self.verify.assert_not_called()
        self.anonymous.assert_not_called()
        self.api.repo.assert_not_called()

    def test_unknown_alias_or_divergent_history_is_not_overwritten(self):
        self.current = 'unknown'
        with self.assertRaisesRegex(ValueError, 'Existing latest'):
            manual.promote_latest(self.api, 'main')
        self.assertFalse(any(call[0] == 'skopeo' for call in self.calls))
        self.current = None
        with patch.object(manual, 'ancestor', return_value=False), self.assertRaisesRegex(ValueError, 'main history'):
            manual.promote_latest(self.api, 'main')
        self.verify.assert_not_called()

    def test_failed_completed_artifact_verification_never_updates_alias(self):
        self.verify.side_effect = RuntimeError('asset mismatch')
        with self.assertRaisesRegex(RuntimeError, 'asset mismatch'):
            manual.promote_latest(self.api, 'main')
        self.assertFalse(any(call[0] == 'skopeo' for call in self.calls))
