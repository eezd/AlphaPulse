"""Exercise real release transitions with temporary originals and fake remote I/O."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
import urllib.parse
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import release
spec = importlib.util.spec_from_file_location('release_recovery', Path(__file__).resolve().parents[1]/'manual-release.py')
manual = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manual)


class FakeAPI(release.API):
    """Keep the production upload/hash checks; replace only HTTP and downloads."""
    def __init__(self, github=False):
        super().__init__('https://api.github.com' if github else 'https://gitea.test/api/v1', 'fixture', github)
        self.headers = {'X-OAuth-Scopes': 'repo,workflow,write:packages'}
        self.releases = {}
        self.blobs = {}
        self.uploads = []
        self.fail_upload = False
        self.head = 'a'*40
        self.before_upload = lambda: None

    def request(self, path, data=None, method=None, **kwargs):
        if path == '/user':
            return {'login': 'eezd'}
        if method == 'POST' and (path.startswith('https://uploads.github.com/') or '/assets?' in path):
            self.before_upload()
            name = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)['name'][0]
            identifier = int(path.split('/releases/')[1].split('/')[0])
            if self.github:
                blob = data
            else:
                boundary = kwargs['content_type'].split('boundary=')[1]
                blob = data.split(b'\r\n\r\n', 1)[1].removesuffix(('\r\n--'+boundary+'--\r\n').encode())
            self.blobs[identifier, name] = blob
            self.uploads.append((identifier, name))
            if self.fail_upload:
                self.fail_upload = False
                raise RuntimeError('upload interrupted after storage')
            return {}
        prefix = '/repos/'+release.REPOSITORY
        if not path.startswith(prefix):
            raise AssertionError(path)
        path = path.removeprefix(prefix)
        if not path:
            return {'permissions': {'push': True}}
        if path.startswith('/branches/'):
            return {'commit': {'id': self.head}}
        if path.startswith('/commits/'):
            return {'sha': self.head}
        if path.startswith('/releases/tags/'):
            return self.releases.get(path.removeprefix('/releases/tags/'))
        if path == '/releases' and method == 'POST':
            identifier = len(self.releases)+1
            value = {**data, 'id': identifier, 'prerelease': False,
                     'upload_url': f'https://uploads.github.com/repos/eezd/AlphaPulse/releases/{identifier}/assets{{?name}}'}
            self.releases[data['tag_name']] = value
            return value
        if path.startswith('/releases/'):
            identifier = int(path.split('/')[2])
            if path.endswith('/assets'):
                return [{'name': name, 'id': (identifier, name)} for item, name in self.blobs if item == identifier]
            if method == 'PATCH':
                value = next(r for r in self.releases.values() if r['id'] == identifier)
                value.update(data)
                return value
        raise AssertionError((path, method))

    def download(self, asset):
        return self.blobs[asset['id']]


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.records = self.root/'releases'
        self.records.mkdir()
        self.apis = (FakeAPI(), FakeAPI(github=True))
        self.value = 'sha256:'+'c'*64
        self.remote_images = {}
        self.image_uploads = []
        def command(*args, **kwargs):
            if args[:2] == ('git', 'show'):
                return '1794225600'
            if args[:2] == ('skopeo', 'copy'):
                self.assert_upload_ready()
                image, tag = args[-1].removeprefix('docker://').rsplit(':', 1)
                self.remote_images[image, tag] = self.value
                self.image_uploads.append((image, tag))
            return ''
        for api in self.apis:
            api.before_upload = self.assert_upload_ready
        patches = [
            patch.object(manual, 'STATE', self.root),
            patch.object(manual, 'clients', return_value=self.apis),
            patch.object(manual, 'run', side_effect=command),
            patch.object(manual, 'ancestor', return_value=True),
            patch.object(manual, 'git_env', return_value={}),
            patch.object(manual, 'synchronize'),
            patch.object(manual, 'registry_login'),
            patch.object(manual, 'registry_digest', side_effect=lambda image, tag: self.remote_images.get((image, tag))),
            patch.object(manual, 'image_digest', return_value=self.value),
            patch.object(manual, 'verify_anonymous'),
            patch.dict(os.environ, {'SOURCE_BRANCH': 'dev', 'SOURCE_SHA': 'a'*40,
                                    'GITHUB_REF': 'refs/heads/main', 'CI_RUN_ID': '123', 'DOCKER_CONFIG': str(self.root)}),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.version = '20261109-aaaaaaaa'
        self.path = self.records/(self.version+'.json')

    def invoke(self):
        with contextlib.redirect_stdout(io.StringIO()):
            manual.execute()

    def record(self):
        return json.loads(self.path.read_text())

    def assert_upload_ready(self):
        record = self.record()
        self.assertEqual(record['phase'], 'publishing')
        checkpoint = manual.verify_checkpoint(self.records/self.version, record)
        self.assertEqual(record['assets'], {k: v for k, v in checkpoint['files'].items() if k != 'candidate.oci'})

    def originals(self, cache, record):
        """Small file fixture only; release transitions and verification remain real."""
        for platform in release.PLATFORMS:
            (cache/release.archive_name(record['version'], platform)).write_bytes(platform.encode())
        (cache/'SHA256SUMS').write_text('fixture checksums')
        (cache/'packages.json').write_text('{}')
        (cache/'candidate.oci').write_bytes(b'fixture OCI')
        release.write_json(cache/'build.json', {'version': record['version'], 'source_sha': record['source_sha'],
                                              'source_branch': record['source_branch'], 'previous_tag': record['previous_tag'],
                                              'images': {'digest': self.value}})
        release.write_json(cache/'checkpoint.json', {'version': record['version'], 'source_sha': record['source_sha'],
                           'files': {p.name: release.digest(p) for p in cache.iterdir() if p.is_file()}})

    def reserve(self):
        with patch.object(manual, 'prepare_artifacts', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.invoke()
        return self.record()

    def test_check_package_failure_or_interrupt_allows_fixed_commit_and_keeps_identity(self):
        # All three happen in package.py; none yields a confirmed package receipt.
        for index, failure in enumerate((subprocess.CalledProcessError(1, ['pytest']),
                                        subprocess.CalledProcessError(1, ['package']), KeyboardInterrupt())):
            with self.subTest(failure=type(failure).__name__):
                for api in self.apis:
                    api.head = 'a'*40
                with patch.object(manual.subprocess, 'run', side_effect=failure):
                    with self.assertRaises(type(failure)):
                        self.invoke()
                before = self.record()
                self.assertEqual(before['phase'], 'building')
                self.assertEqual(before['notes'], 'init')
                self.assertIsNone(before['previous_tag'])
                self.assertTrue(all(api.release(self.version)['draft'] and not api.blobs for api in self.apis))
                fixed_sha = 'bcd'[index]*40
                for api in self.apis:
                    api.head = fixed_sha
                with patch.dict(os.environ, {'SOURCE_SHA': fixed_sha}), patch.object(manual, 'prepare_artifacts', side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        self.invoke()
                self.assertEqual(self.record(), before)

    def test_checkpoint_before_phase_update_blocks_a_fixed_version(self):
        def interrupted(cache, record):
            self.originals(cache, record)
            raise KeyboardInterrupt()
        with patch.object(manual, 'prepare_artifacts', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.invoke()
        self.assertEqual(self.record()['phase'], 'building')
        with patch.dict(os.environ, {'SOURCE_SHA': 'b'*40}):
            with self.assertRaisesRegex(ValueError, 'pending release'):
                self.invoke()

    def test_originals_remote_assets_images_and_legacy_records_block(self):
        record = self.reserve()
        cache = self.records/self.version
        for evidence in ('packages', 'gitea-asset', 'github-asset', 'image', 'legacy'):
            with self.subTest(evidence=evidence):
                if evidence == 'packages':
                    (cache/'packages.json').write_text('{}')
                elif evidence.endswith('asset'):
                    api = self.apis[evidence.startswith('github')]
                    api.blobs[api.release(self.version)['id'], 'original'] = b'original'
                elif evidence == 'image':
                    self.remote_images[release.GITHUB_IMAGE, self.version] = self.value
                else:
                    record.pop('phase')
                self.assertTrue(manual.needs_recovery(record, self.records, self.apis))
                (cache/'packages.json').unlink(missing_ok=True)
                for api in self.apis:
                    api.blobs.clear()
                self.remote_images.clear()

    def test_retrying_old_reservation_cannot_bypass_pending_publication(self):
        self.reserve()
        release.write_json(self.records/'other.json', {'version': 'other', 'phase': 'publishing', 'complete': False})
        with patch.object(manual, 'prepare_artifacts') as prepare:
            with self.assertRaisesRegex(ValueError, 'pending release'):
                self.invoke()
            prepare.assert_not_called()

    def test_partial_upload_resumes_only_missing_assets_without_rebuilding(self):
        def prepare(cache, record):
            self.assertFalse(any(api.uploads for api in self.apis))
            self.originals(cache, record)
        self.apis[0].fail_upload = True
        with patch.object(manual, 'prepare_artifacts', side_effect=prepare) as build:
            with self.assertRaisesRegex(RuntimeError, 'upload interrupted'):
                self.invoke()
            self.assertEqual(self.record()['phase'], 'publishing')
            self.assertTrue((self.records/self.version/'checkpoint.json').exists())
            first = self.apis[0].uploads[0]
            copies = self.image_uploads.copy()
            self.invoke()
            build.assert_called_once()
            self.assertEqual(self.image_uploads, copies)
        self.assertEqual(self.apis[0].uploads.count(first), 1)
        record = self.record()
        self.assertTrue(record['complete'])
        self.assertEqual(record['phase'], 'complete')
        self.assertEqual(len(record['assets']), 9)
        for api in self.apis:
            self.assertFalse(api.release(self.version)['draft'])
            self.assertEqual(len(api.uploads), 9)

    def test_missing_or_corrupt_publishing_originals_fail_without_rebuild(self):
        self.apis[0].fail_upload = True
        with patch.object(manual, 'prepare_artifacts', side_effect=self.originals):
            with self.assertRaises(RuntimeError):
                self.invoke()
        cache = self.records/self.version
        (cache/'SHA256SUMS').write_text('corrupt')
        with patch.object(manual, 'prepare_artifacts') as prepare:
            with self.assertRaisesRegex(ValueError, 'corrupt or missing'):
                self.invoke()
            (cache/'checkpoint.json').unlink()
            with self.assertRaisesRegex(ValueError, 'checkpoint is missing'):
                self.invoke()
            prepare.assert_not_called()

    def test_publishing_retry_cannot_rewrite_frozen_hashes(self):
        self.apis[0].fail_upload = True
        with patch.object(manual, 'prepare_artifacts', side_effect=self.originals):
            with self.assertRaises(RuntimeError):
                self.invoke()
        record = self.record()
        record['image_digest'] = 'sha256:'+'d'*64
        release.write_json(self.path, record)
        with patch.object(manual, 'prepare_artifacts') as prepare:
            with self.assertRaisesRegex(ValueError, 'frozen record'):
                self.invoke()
            self.assertEqual(self.record(), record)
            prepare.assert_not_called()

    def test_same_sha_on_another_branch_keeps_original_identity(self):
        original = self.reserve()
        with patch.dict(os.environ, {'SOURCE_BRANCH': 'feature/example'}), \
             patch.object(manual, 'prepare_artifacts', side_effect=self.originals):
            self.invoke()
        record = self.record()
        for key in ('source_branch', 'source_sha', 'version', 'previous_tag', 'notes'):
            self.assertEqual(record[key], original[key])
        with patch.dict(os.environ, {'SOURCE_BRANCH': 'main'}), patch.object(manual, 'prepare_artifacts') as prepare:
            self.invoke()
            prepare.assert_not_called()

    def test_version_collision_fails_before_building(self):
        self.reserve()
        with patch.dict(os.environ, {'SOURCE_SHA': 'a'*8+'b'*32}), patch.object(manual, 'prepare_artifacts') as prepare:
            with self.assertRaisesRegex(ValueError, 'Version collision'):
                self.invoke()
            prepare.assert_not_called()

    def test_uploaded_image_without_checkpoint_cannot_be_rebuilt(self):
        self.reserve()
        self.remote_images[release.GITEA_IMAGE, self.version] = self.value
        with patch.object(manual, 'prepare_artifacts') as prepare:
            with self.assertRaisesRegex(ValueError, 'image exists without a checkpoint'):
                self.invoke()
            prepare.assert_not_called()


class ArtifactTests(unittest.TestCase):
    def test_actual_preparation_checkpoints_all_tested_originals_before_upload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            downloads = root/'source/downloads'
            downloads.mkdir(parents=True)
            cache = root/'cache'
            cache.mkdir()
            record = {'phase': 'building', 'source_sha': 'a'*40, 'version': '20261109-aaaaaaaa',
                      'source_branch': 'dev', 'previous_tag': None}
            files = {}
            for platform in release.PLATFORMS:
                path = downloads/release.archive_name(record['version'], platform)
                if platform.startswith('linux'):
                    with tarfile.open(path, 'w:gz') as archive:
                        content = b'synthetic package'
                        member = tarfile.TarInfo('target.json')
                        member.size = len(content)
                        archive.addfile(member, io.BytesIO(content))
                else:
                    path.write_bytes(b'synthetic cross-platform archive')
                files[path.name] = {'sha256': release.digest(path), 'size': path.stat().st_size}
            (downloads/'SHA256SUMS').write_text(''.join(f'{v["sha256"]}  {k}\n' for k, v in files.items()))
            release.write_json(downloads/'build.json', {'commit': record['source_sha'], 'version': record['version'],
                                                      'files': files, 'validation': {}})
            manifest = json.dumps({'manifests': [{'platform': {'os': 'linux', 'architecture': arch}}
                                                 for arch in ('amd64', 'arm64')]}).encode()
            value = 'sha256:'+hashlib.sha256(manifest).hexdigest()
            def build_oci(image, destination, *args):
                outer = json.dumps({'manifests': [{'digest': value}]}).encode()
                with tarfile.open(destination, 'w') as archive:
                    for name, content in (('index.json', outer), ('blobs/sha256/'+value.removeprefix('sha256:'), manifest)):
                        member = tarfile.TarInfo(name)
                        member.size = len(content)
                        archive.addfile(member, io.BytesIO(content))
            def process(command, **kwargs):
                self.assertFalse((cache/'checkpoint.json').exists())
                if command[1].endswith('package.py'):
                    self.assertNotIn('GITEA_RELEASE_TOKEN', kwargs['env'])
                    self.assertNotIn('GITHUB_RELEASE_TOKEN', kwargs['env'])
            with patch.object(manual.Path, 'resolve', return_value=root/'source'), \
                 patch.object(manual.subprocess, 'run', side_effect=process), \
                 patch.object(manual, 'build_oci', side_effect=build_oci), patch.object(manual, 'run') as run, \
                 patch.dict(os.environ, {'CI_RUN_ID': '123', 'GITEA_RELEASE_TOKEN': 'fixture', 'GITHUB_RELEASE_TOKEN': 'fixture'}):
                manual.prepare_artifacts(cache, record)
            run.assert_called_once_with('docker', 'load', '--input', str(cache/'candidate.oci'))
            checkpoint = manual.verify_checkpoint(cache, record)
            self.assertEqual(len(checkpoint['files']), 10)
            self.assertEqual(json.loads((cache/'build.json').read_text())['images']['digest'], value)
