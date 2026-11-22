"""Raw OCI index/child verification and per-run registry authentication boundaries."""
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import release


INDEX_TYPE = 'application/vnd.oci.image.index.v1+json'
MANIFEST_TYPE = 'application/vnd.oci.image.manifest.v1+json'


def content_digest(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def raw_json(value):
    # Whitespace is part of the registry content digest, including the final newline.
    return (json.dumps(value, indent=2) + '\n').encode()


class ReleaseImageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / 'host-home'
        self.config = self.root / 'run-docker'
        self.home.mkdir()
        self.config.mkdir()
        (self.config / 'config.json').write_text('{"auths": {"registry.invalid": {"auth": "synthetic"}}}')
        (self.home / '.docker').mkdir()
        (self.home / '.docker/config.json').write_text('{"auths": {"must-not-use": {}}}')
        environment = {'PATH': os.defpath, 'HOME': str(self.home), 'DOCKER_CONFIG': str(self.config),
                       'REGISTRY_AUTH_FILE': str(self.home / 'must-not-use.json'),
                       'XDG_CONFIG_HOME': str(self.home), 'XDG_RUNTIME_DIR': str(self.home),
                       'FORGEJO_RELEASE_TOKEN': 'must-not-inherit'}
        patcher = patch.dict(os.environ, environment, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.reference = 'registry.invalid:5000/team/image:version'
        self.repository = self.reference.rsplit(':', 1)[0]
        self.children = {}
        self.index = {'schemaVersion': 2, 'mediaType': INDEX_TYPE, 'manifests': []}
        for arch in ('amd64', 'arm64'):
            raw = raw_json({'schemaVersion': 2, 'mediaType': MANIFEST_TYPE,
                            'config': {'digest': 'sha256:' + ('a' if arch == 'amd64' else 'b') * 64},
                            'layers': []})
            digest = content_digest(raw)
            self.children[digest] = raw
            self.index['manifests'].append({'digest': digest, 'mediaType': MANIFEST_TYPE,
                                            'platform': {'os': 'linux', 'architecture': arch}})
        self.calls = []
        self.authfiles = []
        self.environments = []
        self.auth_contents = []
        self.auth_modes = []
        self.tagged = {}
        self.fail_reference = None
        self.failure = None
        self.refresh_index()
        runner = patch.object(release, 'run', side_effect=self.command)
        runner.start()
        self.addCleanup(runner.stop)

    def refresh_index(self):
        self.raw = raw_json(self.index)
        self.expected = {'ref': self.reference, 'digest': content_digest(self.raw),
                         'platforms': {d['platform']['os'] + '/' + d['platform']['architecture']: d['digest']
                                       for d in self.index['manifests']}}

    def command(self, *args, **kwargs):
        self.calls.append(args)
        self.assertEqual(args[:4], ('skopeo', 'inspect', '--raw', '--authfile'))
        self.assertEqual(len(args), 6)
        self.assertIs(kwargs['text'], False)
        authfile = Path(args[4])
        self.authfiles.append(authfile)
        self.auth_contents.append(json.loads(authfile.read_text()))
        self.auth_modes.append(stat.S_IMODE(authfile.stat().st_mode))
        env = kwargs['env']
        self.environments.append(env.copy())
        self.assertNotEqual(env['HOME'], str(self.home))
        self.assertEqual(env['XDG_CONFIG_HOME'], env['HOME'])
        self.assertEqual(env['XDG_RUNTIME_DIR'], env['HOME'])
        self.assertEqual(env['DOCKER_CONFIG'], env['HOME'])
        self.assertEqual(env['REGISTRY_AUTH_FILE'], str(authfile))
        self.assertNotIn('FORGEJO_RELEASE_TOKEN', env)
        self.assertFalse((Path(env['HOME']) / '.docker/config.json').exists())
        ref = args[-1].removeprefix('docker://')
        if ref == self.fail_reference:
            raise self.failure
        if ref in (self.reference, self.repository + '@' + content_digest(self.raw)):
            return self.raw
        if ref in self.tagged:
            return self.tagged[ref]
        self.assertTrue(ref.startswith(self.repository + '@'))
        return self.children[ref.split('@')[1]]

    def assert_checked_both_children(self):
        self.assertEqual(len(self.calls), 3)
        self.assertEqual({args[-1] for args in self.calls[1:]},
                         {'docker://' + self.repository + '@' + digest for digest in self.children})
        self.assertEqual(len(set(self.authfiles)), 1)

    def readable_tags(self):
        self.expected['architecture_tags'] = {platform: 'version-'+platform.split('/')[1]
                                              for platform in self.expected['platforms']}
        self.tagged = {self.repository+':'+tag: self.children[self.expected['platforms'][platform]]
                       for platform, tag in self.expected['architecture_tags'].items()}

    def test_readable_versions_verify_three_tags_and_both_children_in_both_auth_modes(self):
        self.readable_tags()
        for anonymous in (False, True):
            with self.subTest(anonymous=anonymous):
                self.calls.clear()
                verify = release.verify_anonymous if anonymous else release.image_digest
                self.assertEqual(verify(self.reference, self.expected), self.expected['digest'])
                self.assertEqual({call[-1] for call in self.calls},
                                 {'docker://'+ref for ref in (self.reference, *self.tagged,
                                  *(self.repository+'@'+value for value in self.children))})
                self.assertEqual(len(self.calls), 5)
                self.assertEqual(self.auth_contents[-1], {'auths': {}} if anonymous else
                                 {'auths': {'registry.invalid': {'auth': 'synthetic'}}})

    def test_missing_swapped_or_corrupt_readable_tags_fail_even_when_index_is_valid(self):
        for anonymous in (False, True):
            for mutation in ('missing-amd64', 'missing-arm64', 'swapped', 'corrupt'):
                with self.subTest(anonymous=anonymous, mutation=mutation):
                    self.readable_tags()
                    if mutation.startswith('missing-'):
                        self.fail_reference = self.repository+':version-'+mutation.removeprefix('missing-')
                        self.failure = subprocess.CalledProcessError(1, ['skopeo'], stderr=b'manifest unknown')
                    elif mutation == 'swapped':
                        values = list(self.tagged.values())
                        self.tagged = dict(zip(self.tagged, reversed(values), strict=True))
                    else:
                        self.tagged[self.repository+':version-arm64'] += b' '
                    verify = release.verify_anonymous if anonymous else release.image_digest
                    with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                        verify(self.reference, self.expected)
                    self.fail_reference = None

    def test_readable_tag_mapping_cannot_redirect_verification_to_other_versions(self):
        self.readable_tags()
        self.expected['architecture_tags']['linux/arm64'] = 'unrelated-arm64'
        with self.assertRaisesRegex(ValueError, 'tag mapping differs'):
            release.verify_anonymous(self.reference, self.expected)

    def test_authenticated_digest_verifies_index_and_both_raw_children(self):
        self.assertEqual(release.image_digest(self.reference), self.expected['digest'])
        self.assert_checked_both_children()
        self.assertEqual(set(self.authfiles), {self.config / 'config.json'})
        self.assertTrue(all(value == {'auths': {'registry.invalid': {'auth': 'synthetic'}}}
                            for value in self.auth_contents))

    def test_digest_reference_is_checked_against_exact_index_bytes(self):
        self.assertEqual(release.image_digest(self.repository + '@' + self.expected['digest']),
                         self.expected['digest'])
        self.assert_checked_both_children()
        self.calls.clear()
        self.reference = self.repository + '@sha256:' + 'f' * 64
        with self.assertRaisesRegex(ValueError, 'Requested image digest differs'):
            release.image_digest(self.reference)
        self.assertEqual(len(self.calls), 1)

    def test_anonymous_verifies_metadata_and_never_uses_existing_auth_or_docker_pull(self):
        self.assertEqual(release.verify_anonymous(self.reference, self.expected), self.expected['digest'])
        self.assert_checked_both_children()
        self.assertTrue(all(value == {'auths': {}} for value in self.auth_contents))
        self.assertEqual(set(self.auth_modes), {0o600})
        self.assertNotIn(self.config / 'config.json', self.authfiles)
        self.assertTrue(all(not path.exists() for path in self.authfiles))
        self.assertTrue(all(not Path(env['HOME']).exists() for env in self.environments))

    def test_anonymous_without_metadata_still_verifies_both_children(self):
        self.assertEqual(release.verify_anonymous(self.reference), self.expected['digest'])
        self.assert_checked_both_children()

    def test_each_anonymous_check_gets_a_new_empty_authfile(self):
        release.verify_anonymous(self.reference)
        first = self.authfiles[0]
        release.verify_anonymous(self.reference)
        self.assertNotEqual(first, self.authfiles[-1])
        self.assertTrue(all(value == {'auths': {}} for value in self.auth_contents))

    def test_authenticated_check_falls_back_only_to_temporary_empty_auth(self):
        for missing in ('unset', 'file'):
            with self.subTest(missing=missing):
                if missing == 'unset':
                    os.environ.pop('DOCKER_CONFIG')
                else:
                    os.environ['DOCKER_CONFIG'] = str(self.root / 'missing-config')
                self.calls.clear()
                self.authfiles.clear()
                self.auth_contents.clear()
                release.image_digest(self.reference)
                self.assert_checked_both_children()
                self.assertTrue(all(value == {'auths': {}} for value in self.auth_contents))
                self.assertTrue(all(not path.exists() for path in self.authfiles))

    def test_corrupt_arm64_raw_manifest_fails_in_both_modes(self):
        arm64 = self.expected['platforms']['linux/arm64']
        self.children[arm64] += b' '
        for verify in (release.image_digest, release.verify_anonymous):
            with self.subTest(verify=verify.__name__):
                with self.assertRaisesRegex(ValueError, 'Image manifest digest differs: linux/arm64'):
                    verify(self.reference)

    def test_missing_arm64_child_is_not_accepted_as_absent_image(self):
        self.fail_reference = self.repository + '@' + self.expected['platforms']['linux/arm64']
        self.failure = subprocess.CalledProcessError(1, ['skopeo'], stderr=b'manifest unknown')
        for verify in (release.image_digest, release.verify_anonymous):
            with self.subTest(verify=verify.__name__):
                with self.assertRaises(subprocess.CalledProcessError) as error:
                    verify(self.reference)
                self.assertIs(error.exception, self.failure)

    def test_missing_arm64_descriptor_fails(self):
        self.index['manifests'].pop()
        self.refresh_index()
        with self.assertRaisesRegex(ValueError, 'Unexpected image platforms'):
            release.image_digest(self.reference)

    def test_duplicate_and_extra_platforms_fail(self):
        original = copy.deepcopy(self.index['manifests'])
        for platform in ({'os': 'linux', 'architecture': 'amd64'},
                         {'os': 'linux', 'architecture': 's390x'},
                         {'os': 'unknown', 'architecture': 'unknown'}):
            with self.subTest(platform=platform):
                self.index['manifests'] = original + [{'platform': platform, 'digest': 'sha256:' + 'c' * 64}]
                self.refresh_index()
                with self.assertRaisesRegex(ValueError, 'Unexpected or duplicate image platform'):
                    release.verify_anonymous(self.reference)

    def test_both_platforms_cannot_point_to_same_child(self):
        self.index['manifests'][1]['digest'] = self.index['manifests'][0]['digest']
        self.refresh_index()
        with self.assertRaisesRegex(ValueError, 'distinct manifests'):
            release.image_digest(self.reference)

    def test_invalid_child_digest_is_rejected_before_fetching_children(self):
        self.index['manifests'][1]['digest'] = 'sha256:not-a-digest'
        self.refresh_index()
        with self.assertRaisesRegex(ValueError, 'Invalid image manifest digest: linux/arm64'):
            release.image_digest(self.reference)
        self.assertEqual(len(self.calls), 1)

    def test_build_metadata_top_digest_mismatch_fails(self):
        self.expected['digest'] = 'sha256:' + 'f' * 64
        with self.assertRaisesRegex(ValueError, 'Image digest differs from build metadata'):
            release.verify_anonymous(self.reference, self.expected)
        self.assertEqual(len(self.calls), 1)

    def test_build_metadata_platform_mapping_must_match_exactly(self):
        for change in ('wrong-digest', 'missing', 'extra'):
            with self.subTest(change=change):
                expected = copy.deepcopy(self.expected)
                if change == 'wrong-digest':
                    expected['platforms']['linux/arm64'] = 'sha256:' + 'f' * 64
                elif change == 'missing':
                    del expected['platforms']['linux/arm64']
                else:
                    expected['platforms']['linux/s390x'] = 'sha256:' + 'f' * 64
                with self.assertRaisesRegex(ValueError, 'Image platforms differ from build metadata'):
                    release.verify_anonymous(self.reference, expected)

    def test_invalid_index_json_or_non_index_fails(self):
        for raw in (b'not json\n', raw_json([]), raw_json({'schemaVersion': 2, 'config': {}, 'layers': []})):
            with self.subTest(raw=raw):
                self.raw = raw
                with self.assertRaises(ValueError):
                    release.image_digest(self.reference)

    def test_digest_consistent_but_malformed_child_fails(self):
        for raw in (b'not json\n', raw_json({'schemaVersion': 2, 'manifests': []})):
            with self.subTest(raw=raw):
                digest = content_digest(raw)
                self.children[digest] = raw
                self.index['manifests'][1]['digest'] = digest
                self.refresh_index()
                with self.assertRaises(ValueError):
                    release.image_digest(self.reference)

    def test_network_and_auth_errors_propagate_without_missing_fallback(self):
        self.fail_reference = self.reference
        for message in (b'unauthorized', b'TLS handshake timeout', b'manifest unknown'):
            self.failure = subprocess.CalledProcessError(1, ['skopeo'], stderr=message)
            for verify in (release.image_digest, release.verify_anonymous):
                with self.subTest(message=message, verify=verify.__name__):
                    self.calls.clear()
                    with self.assertRaises(subprocess.CalledProcessError) as error:
                        verify(self.reference)
                    self.assertIs(error.exception, self.failure)
                    self.assertEqual(len(self.calls), 1)
                    self.assertFalse(Path(self.environments[-1]['HOME']).exists())


if __name__ == '__main__':
    unittest.main()
