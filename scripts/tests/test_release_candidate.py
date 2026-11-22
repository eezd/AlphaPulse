"""Candidate boundaries use fixed Git exports, isolated Docker specs and exact cleanup."""
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
spec = importlib.util.spec_from_file_location('candidate_test', Path(__file__).resolve().parents[1]/'release-candidate.py')
candidate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(candidate)


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'CI_RUN_ID': '123', 'CI_RUN_ATTEMPT': '2'}).start()

    def test_invalid_identity_cannot_select_any_docker_resources(self):
        for bad in ('', '0', '-1', '1;echo bad', '../other'):
            with self.subTest(bad=bad), patch.dict(os.environ, {'CI_RUN_ATTEMPT': bad}), \
                 patch.object(candidate, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'run/attempt'):
                    candidate.cleanup()
                run.assert_not_called()

    def test_workflow_always_cleans_private_paths_even_when_resource_cleanup_fails(self):
        workflow = (Path(__file__).resolve().parents[2]/'.forgejo/workflows/release.yml').read_text()
        step = workflow.split('- name: Clean only this run', 1)[1]
        self.assertIn('if: always()', step)
        self.assertIn('CI_RUN_ATTEMPT: ${{ forgejo.run_attempt }}', step)
        script = step.split('run: |\n', 1)[1]
        import textwrap
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root/'python3'
            executable.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CALLS"\nexit 7\n')
            executable.chmod(0o700)
            auth = root/'auth'; auth.mkdir()
            context = root/'alphapulse-release-123-2.json'; context.write_text('synthetic')
            neighbor = root/'other-attempt'; neighbor.mkdir()
            script = textwrap.dedent(script).replace('${{ runner.temp }}', directory).replace('${{ forgejo.run_id }}', '123').replace('${{ forgejo.run_attempt }}', '2')
            result = subprocess.run(['bash', '-c', script], env={'PATH': directory+':'+os.defpath,
                                    'HOME': directory, 'DOCKER_CONFIG': str(auth), 'CALLS': str(root/'calls')})
            self.assertEqual(result.returncode, 7)
            self.assertEqual((root/'calls').read_text().splitlines(), ['scripts/release-candidate.py', '--cleanup'])
            self.assertFalse(auth.exists())
            self.assertFalse(context.exists())
            self.assertTrue(neighbor.exists())

    def test_cleanup_selects_only_current_attempt_and_reserved_local_references(self):
        calls = []
        names = candidate.resource_names()
        def command(*args):
            calls.append(args)
            if args[1:3] == ('ps', '-aq'):
                return 'owned-container'
            if args[1:4] == ('network', 'ls', '-q'):
                return 'owned-network'
            if args[1:4] == ('volume', 'ls', '-q'):
                return 'owned-volume'
            if args[1:3] == ('buildx', 'ls'):
                return names['builder']+'\nother-builder'
            if args[1:3] == ('context', 'ls'):
                return names['context']+'\nother-context'
            if args[1:4] == ('image', 'ls', '-q'):
                return 'owned-image'
            return ''
        with patch.object(candidate, 'run', side_effect=command):
            candidate.cleanup()
        for args in calls:
            self.assertNotIn('prune', args)
            if '--filter' in args and args[1] != 'image':
                self.assertEqual(args[-1], 'label=io.alphapulse.ci.run=release-123-2')
        self.assertIn(('docker', 'rm', '-fv', 'owned-container'), calls)
        self.assertIn(('docker', 'volume', 'rm', 'owned-volume'), calls)
        self.assertIn(('docker', 'buildx', 'rm', names['builder']), calls)
        self.assertFalse(any('other-' in str(arg) for args in calls for arg in args))

    def test_builder_uses_attempt_and_separate_tls_context(self):
        with patch.object(candidate, 'run', return_value='') as run:
            name = candidate.ensure_builder()
        self.assertEqual(name, 'alphapulse-release-123-2')
        self.assertEqual(run.call_args_list[-1].args[-1], name+'-context')
        self.assertIn('--bootstrap', run.call_args_list[-1].args)

    def test_fixed_exports_exclude_git_and_untracked_data_and_set_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {'PATH': os.defpath, 'HOME': directory, 'GIT_CONFIG_NOSYSTEM': '1'}
            def git(*args):
                return subprocess.check_output(['git', '-C', directory, *args], env=env, text=True).strip()
            git('init', '-q')
            (root/'source.txt').write_text('frozen\n')
            git('add', 'source.txt')
            git('-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')
            sha = git('rev-parse', 'HEAD')
            (root/'private-untracked').write_text('synthetic-only')
            (root/'source.txt').write_text('modified after freeze\n')
            for untrusted in (True, False):
                target = root/('candidate.tar' if untrusted else 'control.tar')
                candidate.export_tree(root, sha, target, candidate=untrusted)
                with tarfile.open(target) as tree:
                    self.assertEqual(tree.getnames(), ['source.txt'])
                    self.assertEqual(tree.extractfile('source.txt').read(), b'frozen\n')
                    entry = tree.getmember('source.txt')
                    self.assertEqual(entry.uid, 10001 if untrusted else 0)
                    self.assertEqual(entry.mode, 0o644 if untrusted else 0o444)
            with self.assertRaisesRegex(ValueError, 'frozen SHA'):
                candidate.export_tree(root, 'a'*40, root/'bad.tar', candidate=True)

    def test_build_spec_never_forwards_controller_credentials_or_mounts(self):
        def export(repo, sha, destination, **kwargs):
            with tarfile.open(destination, 'w') as tree:
                content = b'FROM synthetic-tools\n'
                member = tarfile.TarInfo('deploy/Dockerfile.release-candidate')
                member.size = len(content)
                tree.addfile(member, io.BytesIO(content))
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {'FORGEJO_RELEASE_TOKEN': 'synthetic-token', 'DOCKER_CONFIG': '/private-auth',
                                     'DOCKER_CERT_PATH': '/private-certificates', 'HOME': '/private-home'}), \
             patch.object(candidate, 'export_tree', side_effect=export), \
             patch.object(candidate, 'ensure_builder', return_value='owned-builder'), \
             patch.object(candidate, 'copy_tree'), patch.object(candidate, 'receive_artifacts'), \
             patch.object(candidate.subprocess, 'run'), patch.object(candidate, 'run', return_value='') as run:
            candidate.build_packages('/source', '/control', 'a'*40, 'b'*40, '20261121-aaaaaaaa', Path(directory))
        create = run.call_args_list[-1].args
        self.assertEqual(create[:2], ('docker', 'create'))
        self.assertIn('--cap-drop=ALL', create)
        self.assertIn('--security-opt=no-new-privileges', create)
        for forbidden in ('--privileged', '--pid', '--mount', '-v', '--volume', '--env-file', 'synthetic-token',
                          '/private-auth', '/private-certificates', '/private-home'):
            self.assertNotIn(forbidden, create)
        envs = [create[index+1] for index, arg in enumerate(create) if arg == '--env']
        self.assertEqual(envs, ['HOME=/home/candidate', 'TMPDIR=/tmp', 'ALPHAPULSE_BUILD_SHA='+'a'*40,
                                'ALPHAPULSE_BUILD_VERSION=20261121-aaaaaaaa'])

    def test_artifact_receiver_accepts_only_complete_flat_regular_files(self):
        version = '20261121-aaaaaaaa'
        expected = {candidate.archive_name(version, platform) for platform in candidate.PLATFORMS} | {'build.json', 'SHA256SUMS'}
        for corruption in (None, 'symlink', 'duplicate', 'missing', 'extra', 'oversize'):
            with self.subTest(corruption=corruption), tempfile.TemporaryDirectory() as directory:
                raw = io.BytesIO()
                with tarfile.open(fileobj=raw, mode='w') as tree:
                    names = sorted(expected - ({'build.json'} if corruption == 'missing' else set()))
                    if corruption == 'duplicate':names += [names[0]]
                    if corruption == 'extra':names += ['unexpected']
                    for name in names:
                        item = tarfile.TarInfo('downloads/'+name)
                        item.size = 1
                        if name == 'build.json' and corruption == 'symlink':
                            item.type = tarfile.SYMTYPE
                            item.linkname = '/private-auth/config.json'
                            item.size = 0
                        tree.addfile(item, io.BytesIO(b'x') if item.isfile() else None)
                raw.seek(0)
                process = Mock(stdout=raw)
                process.wait.return_value = 0
                process.poll.return_value = 0
                with patch.object(candidate.subprocess, 'Popen', return_value=process), \
                     patch.object(candidate, 'UPLOAD_LIMIT', 1 if corruption == 'oversize' else 90_000_000):
                    if corruption is None:
                        candidate.receive_artifacts('owned', version, Path(directory))
                        self.assertEqual({p.name for p in Path(directory).iterdir()}, expected)
                    else:
                        with self.assertRaisesRegex(ValueError, 'artifact'):
                            candidate.receive_artifacts('owned', version, Path(directory))
