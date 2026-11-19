#!/usr/bin/env python3
"""Publish and verify exact Forgejo release artifacts."""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request


REPOSITORY = 'eezd/AlphaPulse'
FORGEJO_IMAGE = 'git.134333.xyz/eezd/alphapulse'
FORGEJO_GIT_URL = f'https://{FORGEJO_IMAGE.split("/")[0]}/{REPOSITORY}.git'
PLATFORMS = ('linux/amd64', 'linux/arm64', 'windows/amd64', 'windows/arm64', 'darwin/amd64', 'darwin/arm64')
UPLOAD_LIMIT = 90_000_000


def run(*command, **kwargs):
    return subprocess.check_output(command, text=True, **kwargs).strip()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix('.next')
    with temporary.open('w') as target:
        target.write(json.dumps(value, indent=2, sort_keys=True) + '\n')
        target.flush()
        os.fsync(target.fileno())
    temporary.replace(path)


def archive_name(version, platform):
    return f'AlphaPulse_{version}_{platform.replace("/", "_")}' + ('.zip' if platform.startswith('windows/') else '.tar.gz')


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, *args):
        redirected = super().redirect_request(request, *args)
        if redirected and urllib.parse.urlsplit(request.full_url)[:2] != urllib.parse.urlsplit(redirected.full_url)[:2]:
            redirected.remove_header('Authorization')
        return redirected


class API:
    def __init__(self, root, token):
        require(token, f'Missing credential for {root}')
        self.root, self.token = root.rstrip('/'), token
        self.opener = urllib.request.build_opener(SafeRedirect())
        self.headers = {}

    def request(self, path, data=None, method=None, *, raw=False, content_type=None, missing=False):
        url = path if path.startswith('https://') or path.startswith('http://') else self.root + path
        require(urllib.parse.urlsplit(url)[:2] == urllib.parse.urlsplit(self.root)[:2]
                and urllib.parse.urlsplit(url).scheme == 'https',
                'Refusing credential-bearing request outside the HTTPS API origin')
        headers = {'User-Agent': 'AlphaPulse-Release', 'Authorization': 'token ' + self.token}
        if data is not None and not isinstance(data, bytes):
            data = json.dumps(data).encode()
            content_type = 'application/json'
        if content_type:
            headers['Content-Type'] = content_type
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=120) as response:
                self.headers = response.headers
                body = response.read()
        except urllib.error.HTTPError as error:
            if missing and error.code == 404:
                return None
            raise RuntimeError(f'{method or "GET"} {urllib.parse.urlsplit(url).path}: HTTP {error.code}') from error
        return body if raw else (json.loads(body) if body else None)

    def repo(self, path='', **kwargs):
        return self.request('/repos/' + REPOSITORY + path, **kwargs)

    def release(self, version):
        return self.repo('/releases/tags/' + version, missing=True)

    def pages(self, path, *, repo=False):
        request = self.repo if repo else self.request
        separator = '&' if '?' in path else '?'
        seen = set()
        for page in range(1, 1001):
            items = request(f'{path}{separator}page={page}&limit=50')
            require(isinstance(items, list), 'Expected a paginated Forgejo list')
            if not items:
                return
            for item in items:
                require(item['id'] not in seen, 'Repeated item in Forgejo pagination')
                seen.add(item['id'])
                yield item
        raise ValueError('Forgejo list exceeds the pagination limit')

    def assets(self, release):
        return list(self.pages(f'/releases/{release["id"]}/assets', repo=True))

    def download(self, asset):
        url = asset['browser_download_url']
        require(urllib.parse.urlsplit(url).netloc in {
            urllib.parse.urlsplit(self.root).netloc, 'git.134333.xyz',
        }, 'Unexpected Forgejo release download host')
        # Use the configured Forgejo origin for authenticated downloads.
        public = urllib.parse.urlsplit(url)
        url = self.root.removesuffix('/api/v1') + public.path + ('?' + public.query if public.query else '')
        request = urllib.request.Request(url, headers={'Authorization': 'token ' + self.token,
                                                       'User-Agent': 'AlphaPulse-Release'})
        with self.opener.open(request, timeout=120) as response:
            return response.read()

    def asset_bytes(self, release, name):
        matches = [a for a in self.assets(release) if a['name'] == name]
        require(len(matches) == 1, f'Expected exactly one release asset: {name}')
        return self.download(matches[0])

    def upload(self, release, path):
        path = Path(path)
        existing = [a for a in self.assets(release) if a['name'] == path.name]
        if existing:
            require(len(existing) == 1 and hashlib.sha256(self.download(existing[0])).hexdigest() == digest(path),
                    f'Existing asset conflicts: {path.name}')
            return
        require(release['draft'] or path.name == 'release-complete.json',
                f'Cannot add files to a published release: {path.name}')
        require(path.stat().st_size < UPLOAD_LIMIT, f'Asset exceeds CDN budget: {path.name}')
        boundary = 'alphapulse-' + secrets.token_hex(12)
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="attachment"; '
                f'filename="{path.name}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
        body += path.read_bytes() + f'\r\n--{boundary}--\r\n'.encode()
        self.repo(f'/releases/{release["id"]}/assets?name={urllib.parse.quote(path.name)}',
                  data=body, method='POST', content_type=f'multipart/form-data; boundary={boundary}')
        require(hashlib.sha256(self.asset_bytes(release, path.name)).hexdigest() == digest(path),
                f'Uploaded asset verification failed: {path.name}')


def client():
    require(os.environ.get('FORGEJO_RELEASE_TOKEN'), 'Configure the RELEASE_TOKEN Forgejo secret mapped to FORGEJO_RELEASE_TOKEN before releasing')
    return API(os.environ['FORGEJO_API_URL'], os.environ['FORGEJO_RELEASE_TOKEN'])


def git_env(api):
    authorization = base64.b64encode(('eezd:' + api.token).encode()).decode()
    return {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_CONFIG_COUNT': '3',
            'GIT_CONFIG_KEY_0': 'credential.helper', 'GIT_CONFIG_VALUE_0': '',
            'GIT_CONFIG_KEY_1': f'http.{FORGEJO_GIT_URL}.extraHeader',
            'GIT_CONFIG_VALUE_1': 'Authorization: Basic ' + authorization,
            'GIT_CONFIG_KEY_2': 'http.followRedirects', 'GIT_CONFIG_VALUE_2': 'false'}


def registry_login():
    subprocess.run(['docker', 'login', FORGEJO_IMAGE.split('/')[0], '--username', 'eezd', '--password-stdin'],
                   input=os.environ['FORGEJO_RELEASE_TOKEN'], text=True, check=True)


def registry_digest(image, reference):
    # Docker's standard registry client handles challenges and the public CDN.
    result = subprocess.run(['docker', 'buildx', 'imagetools', 'inspect', image + '@' + reference
                             if reference.startswith('sha256:') else image + ':' + reference,
                             '--format', '{{json .Manifest}}'], capture_output=True, text=True)
    if result.returncode:
        if 'manifest unknown' in result.stderr.lower() or 'name unknown' in result.stderr.lower() or result.stderr.rstrip().endswith(': not found'):
            return None
        raise RuntimeError('Registry inspection failed: ' + result.stderr.strip())
    value = json.loads(result.stdout)['digest']
    require(re.fullmatch(r'sha256:[0-9a-f]{64}', value), 'Invalid registry content digest')
    return value


def image_digest(reference):
    raw = run('docker', 'buildx', 'imagetools', 'inspect', reference, '--raw')
    manifest = json.loads(raw)
    platforms = {(d['platform']['os'], d['platform']['architecture']) for d in manifest['manifests']}
    require(platforms == {('linux', 'amd64'), ('linux', 'arm64')}, 'Unexpected image platforms')
    image, separator, requested = reference.partition('@')
    if not separator:
        image, requested = reference.rsplit(':', 1)
    value = registry_digest(image, requested)
    require(value, 'Registry returned no image digest')
    return value


def verify_anonymous(image):
    with tempfile.TemporaryDirectory(prefix='alphapulse-anonymous-') as directory:
        env = {**os.environ, 'DOCKER_CONFIG': directory}
        run('docker', 'pull', image, env=env)
