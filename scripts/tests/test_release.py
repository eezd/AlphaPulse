"""Release baseline, Actions source resolution and credential isolation contracts."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
spec = importlib.util.spec_from_file_location('manual', Path(__file__).resolve().parents[1]/'manual-release.py')
manual = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manual)
resolver = manual.resolver


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repository = Path(self.temporary.name)
        self.git('init', '--initial-branch=main')
        self.git('config', 'user.name', 'eezd')
        self.git('config', 'user.email', 'eezd@outlook.com')
        self.tree = self.git('mktree', input='')
        self.nodes = {}
        for name, parents in [('A', []), ('b1', ['A']), ('b2', ['b1']), ('B', ['b2']),
                              ('m1', ['B']), ('M', ['m1']), ('F', ['A']), ('T', ['M', 'F']), ('X', [])]:
            args = ['commit-tree', self.tree, '-m', name]
            for parent in parents:
                args.extend(['-p', self.nodes[parent]])
            self.nodes[name] = self.git(*args)
        self.patcher = patch.object(manual, 'run', side_effect=lambda command, *args: self.git(*args))
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def git(self, *args, **kwargs):
        return subprocess.check_output(['git', '-C', str(self.repository), *args], text=True, **kwargs).strip()

    def record(self, node, *, branch='dev', version=None, completed_at=1, complete=True):
        return {'complete': complete, 'source_branch': branch, 'source_sha': self.nodes[node],
                'completed_at': completed_at, 'version': version or node}

    def test_first_release_notes_are_exact_init(self):
        self.assertEqual(manual.release_notes(None, self.nodes['T']), 'init')

    def test_other_branch_and_incomplete_records_are_not_baselines(self):
        records = [self.record('T', branch='main'), self.record('T', complete=False)]
        self.assertIsNone(manual.select_baseline(records, 'dev', self.nodes['T']))

    def test_rewritten_branch_cannot_look_like_first_release(self):
        with self.assertRaisesRegex(ValueError, 'baseline'):
            manual.select_baseline([self.record('X')], 'dev', self.nodes['T'])

    def test_merge_graph_uses_parent_distance_instead_of_commit_count(self):
        selected = manual.select_baseline([self.record('A'), self.record('B'), self.record('X')], 'dev', self.nodes['T'])
        self.assertEqual(selected['source_sha'], self.nodes['A'])
        self.assertGreater(int(self.git('rev-list', '--count', self.nodes['A']+'..'+self.nodes['T'])),
                           int(self.git('rev-list', '--count', self.nodes['B']+'..'+self.nodes['T'])))

    def test_equal_distances_use_completion_time_then_tag(self):
        records = [self.record('M', version='z', completed_at=2), self.record('F', version='a', completed_at=1)]
        self.assertEqual(manual.select_baseline(records, 'dev', self.nodes['T'])['version'], 'z')
        records[1]['completed_at'] = 2
        self.assertEqual(manual.select_baseline(records, 'dev', self.nodes['T'])['version'], 'a')


class ClientTests(unittest.TestCase):
    def test_native_attachment_readback_uses_one_unpaginated_request(self):
        import release
        api = release.API('https://git.134333.xyz/api/v1', 'fixture-only')
        attachments = [{'id': 1, 'name': 'build.json', 'browser_download_url': 'https://git.134333.xyz/attachments/fixture'}]
        with patch.object(api, 'repo', return_value=attachments) as request, patch.object(api, 'download', return_value=b'metadata'):
            self.assertEqual(api.asset_bytes({'id': 2}, 'build.json'), b'metadata')
        request.assert_called_once_with('/releases/2/assets')

    def test_native_attachment_list_rejects_non_list_response(self):
        import release
        api = release.API('https://git.134333.xyz/api/v1', 'fixture-only')
        with patch.object(api, 'repo', return_value={'assets': []}):
            with self.assertRaisesRegex(ValueError, 'Expected a Forgejo attachment list'):
                api.assets({'id': 2})

    def test_paginated_endpoints_still_reject_repeated_ids(self):
        import release
        api = release.API('https://git.134333.xyz/api/v1', 'fixture-only')
        with patch.object(api, 'repo', return_value=[{'id': 1}]):
            with self.assertRaisesRegex(ValueError, 'Repeated item in Forgejo pagination'):
                list(api.pages('/releases', repo=True))

    def test_workflow_secret_mapping_uses_a_legal_forgejo_name(self):
        import re
        workflow = (Path(__file__).resolve().parents[2]/'.forgejo/workflows/release.yml').read_text()
        mappings = dict(re.findall(r'(?m)^\s+(\w+):\s+\$\{\{\s*secrets\.(\w+)\s*\}\}', workflow))
        self.assertEqual(mappings['FORGEJO_RELEASE_TOKEN'], 'RELEASE_TOKEN')
        self.assertNotIn('GITHUB_TOKEN', mappings)
        for name in mappings.values():
            self.assertRegex(name, r'(?i)^[A-Z_][A-Z0-9_]*$')
            self.assertNotRegex(name, r'(?i)^(FORGEJO_|GITEA_|GITHUB_|[0-9])')

    def test_missing_account_secret_fails_before_network_access(self):
        import release
        with patch.dict(os.environ, {'FORGEJO_RELEASE_TOKEN': ''}), patch.object(release, 'API') as api:
            with self.assertRaisesRegex(ValueError, 'RELEASE_TOKEN.*FORGEJO_RELEASE_TOKEN'):
                release.client()
            api.assert_not_called()


class ResolverTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.env = {'HOME': str(self.home), 'SOURCE_BRANCH': 'feature/demo', 'SOURCE_SHA': '',
                    'CONTROL_REF': 'refs/heads/main', 'CONTROL_SHA': 'c'*40, 'CI_RUN_ID': '123',
                    'FORGEJO_RELEASE_TOKEN': 'synthetic-secret', 'RELEASE_CONTEXT': str(self.home/'context.json'),
                    'GITHUB_OUTPUT': str(self.home/'output')}
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, self.env, clear=True).start()
        self.head = 'a'*40
        self.requests = patch.object(resolver, 'request', side_effect=self.response).start()

    def response(self, path, token):
        self.assertEqual(token, 'synthetic-secret')
        if '/branches/' in path:
            return {'commit': {'id': self.head}}
        return {'commit': {'committer': {'date': '2026-11-09T23:00:00-05:00'}}}

    def test_empty_sha_resolves_execution_time_head_once_with_utc_date(self):
        self.head = 'b'*40  # A queued request resolves only now, not at button click.
        context = resolver.resolve()
        self.assertEqual(context['source_sha'], 'b'*40)
        self.assertEqual(context['version'], '20261110-bbbbbbbb')
        self.assertEqual(context['release_inputs'], {'source_branch': 'feature/demo', 'source_sha': ''})
        self.assertEqual(self.requests.call_count, 2)
        self.assertIn('/branches/feature%2Fdemo', self.requests.call_args_list[0].args[0])
        self.assertEqual(str(resolver.uuid.UUID(context['release_request_id'])), context['release_request_id'])
        self.head = 'd'*40
        self.assertEqual(context['source_branch_head'], 'b'*40)

    def test_explicit_sha_is_fixed_independently_of_current_head(self):
        os.environ['SOURCE_SHA'] = 'd'*40
        context = resolver.resolve()
        self.assertEqual(context['source_sha'], 'd'*40)
        self.assertEqual(context['source_branch_head'], 'a'*40)
        self.assertTrue(self.requests.call_args.args[0].endswith('/git/commits/'+'d'*40))

    def test_bad_input_or_ci_identity_stops_before_api(self):
        for key, value in [('SOURCE_BRANCH', '-bad'), ('SOURCE_BRANCH', 'main bad'), ('SOURCE_SHA', 'HEAD'),
                           ('SOURCE_SHA', 'a'*39), ('CONTROL_REF', 'refs/heads/dev'), ('CI_RUN_ID', '0')]:
            with self.subTest(key=key, value=value), patch.dict(os.environ, {key: value}):
                self.requests.reset_mock()
                with self.assertRaises(ValueError):
                    resolver.resolve()
                self.requests.assert_not_called()

    def test_missing_secret_never_falls_back_to_home(self):
        token = self.home/'.config/forgejo/token'
        token.parent.mkdir(parents=True)
        token.write_text('must-not-read')
        with patch.dict(os.environ, {'FORGEJO_RELEASE_TOKEN': ''}), patch.object(resolver.Path, 'read_text') as read:
            with self.assertRaisesRegex(ValueError, 'RELEASE_TOKEN'):
                resolver.resolve()
            self.requests.assert_not_called()
            read.assert_not_called()

    def test_outputs_contain_frozen_identity_not_tokens(self):
        import contextlib
        import io
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            resolver.main()
        context = json.loads((self.home/'context.json').read_text())
        self.assertEqual(context['source_sha'], 'a'*40)
        self.assertIn('source_sha='+'a'*40, (self.home/'output').read_text())
        self.assertNotIn('synthetic-secret', output.getvalue())

    def test_missing_branch_lookup_propagates_without_dispatch(self):
        self.requests.side_effect = ValueError('Forgejo source lookup failed: HTTP 404')
        with self.assertRaisesRegex(ValueError, 'HTTP 404'):
            resolver.resolve()
        self.assertEqual(self.requests.call_count, 1)
