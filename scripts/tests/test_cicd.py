"""Verify publication gates, immutable identities and Git mirror reconciliation."""

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import common as ci
import mirror


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
