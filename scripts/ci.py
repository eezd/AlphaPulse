#!/usr/bin/env python3
"""Build, verify and publish immutable AlphaPulse development images."""

import argparse
import base64
import contextlib
from datetime import UTC, datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import zlib
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = 'git.134333.xyz'
REPOSITORY = 'eezd/AlphaPulse'
SOURCE = f'https://{REGISTRY}/{REPOSITORY}.git'
IMAGES = {part: f'{REGISTRY}/eezd/dev/{part}/alphapulse' for part in ('backend', 'web')}
LOCK_DIR = Path('/docker/gitea/mirrors/AlphaPulse')
LABEL = 'io.alphapulse.ci.run'


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(*args, input=None, env=None):
    result = subprocess.run(list(map(str, args)), input=input, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        # Callers never pass credentials as command arguments.
        raise RuntimeError(f'{args[0]} failed ({result.returncode}): {result.stderr[-6000:]}{result.stdout[-6000:]}')
    return result.stdout.strip()


def save(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


@contextlib.contextmanager
def locked(path):
    with Path(path).open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def version(commit, timestamp):
    require(re.fullmatch('[0-9a-f]{40}', commit), 'Expected a complete commit SHA')
    return datetime.fromtimestamp(int(timestamp), UTC).strftime('%Y%m%d') + '-' + commit[:8]


def metadata():
    commit = run('git', 'rev-parse', 'HEAD')
    require(os.environ.get('GITEA_SHA', commit) == commit, 'Checkout differs from the event commit')
    return {'commit': commit, 'version': version(commit, run('git', 'show', '-s', '--format=%ct', commit)),
            'built_at': datetime.now(UTC).isoformat(), 'platform': 'linux/amd64', 'images': {}}


def request_json(url, token=None, data=None, method=None):
    headers = {'User-Agent': 'AlphaPulse-CI', 'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'token ' + token
    if data is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(data).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=30) as response:
        return json.load(response)


def branch_head():
    return request_json(f'https://{REGISTRY}/api/v1/repos/{REPOSITORY}/branches/dev')['commit']['id']


def registry_json(path, token=None):
    url = f'https://{REGISTRY}/v2/{path}'
    headers = {'User-Agent': 'AlphaPulse-CI',
               'Accept': 'application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
            raw = response.read()
            return json.loads(raw), response.headers.get('Docker-Content-Digest') or 'sha256:' + hashlib.sha256(raw).hexdigest()
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        if error.code != 401 or token:
            raise
        challenge = error.headers.get('WWW-Authenticate', '')
        fields = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        require(fields.get('realm', '').startswith(f'https://{REGISTRY}/'), 'Unexpected registry authentication endpoint')
        auth_headers = {'User-Agent': 'AlphaPulse-CI'}
        secret = os.environ.get('ALPHAPULSE_GITEA_TOKEN')
        if secret:
            auth_headers['Authorization'] = 'Basic ' + base64.b64encode(('eezd:' + secret).encode()).decode()
        auth_url = fields['realm'] + '?' + urllib.parse.urlencode({k: fields[k] for k in ('service', 'scope') if k in fields})
        with urllib.request.urlopen(urllib.request.Request(auth_url, headers=auth_headers), timeout=30) as response:
            bearer = json.load(response)['token']
        return registry_json(path, bearer)


def existing_image(part, build):
    repository = IMAGES[part].split('/', 1)[1]
    result = registry_json(f'{repository}/manifests/{build["version"]}')
    if result is None:
        return None
    manifest, digest = result
    require('config' in manifest, 'Expected a single Linux amd64 image manifest')
    config = registry_json(f'{repository}/blobs/{manifest["config"]["digest"]}')[0]
    labels = config['config'].get('Labels') or {}
    require(labels.get('org.opencontainers.image.revision') == build['commit'], 'Existing version belongs to a different commit')
    require(labels.get('org.opencontainers.image.version') == build['version'], 'Existing image version differs')
    require(config.get('architecture') == 'amd64' and config.get('os') == 'linux', 'Existing image platform differs')
    return IMAGES[part] + '@' + digest


def wait_command(*command, attempts=60):
    for attempt in range(attempts):
        try:
            return run(*command)
        except RuntimeError:
            if attempt == attempts - 1:
                raise
            time.sleep(1)


def cleanup(label, images=()):
    for identifier in run('docker', 'ps', '-aq', '--filter', f'label={LABEL}={label}').split():
        run('docker', 'rm', '-fv', identifier)
    for identifier in run('docker', 'network', 'ls', '-q', '--filter', f'label={LABEL}={label}').split():
        run('docker', 'network', 'rm', identifier)
    for image in images:
        if image.startswith('alphapulse-ci:'):
            run('docker', 'image', 'rm', image)


def smoke(build, label):
    network = 'ap-ci-' + label
    names = {part: network + '-' + part for part in ('db', 'redis', 'api', 'web')}
    # Pull before entering the internal network; no runtime service can reach external I/O.
    for image in ('postgres:18-alpine', 'redis:8-alpine'):
        run('docker', 'pull', image)
    run('docker', 'network', 'create', '--internal', '--label', f'{LABEL}={label}', network)
    common = ['--network', network, '--label', f'{LABEL}={label}']
    try:
        run('docker', 'run', '-d', '--name', names['db'], *common, '-e', 'POSTGRES_PASSWORD=ci-only',
            '-e', 'POSTGRES_USER=ci', '-e', 'POSTGRES_DB=ci', 'postgres:18-alpine')
        run('docker', 'run', '-d', '--name', names['redis'], *common, 'redis:8-alpine')
        wait_command('docker', 'exec', names['db'], 'pg_isready', '-U', 'ci', '-d', 'ci')
        run('docker', 'exec', names['db'], 'psql', '-U', 'ci', '-d', 'ci', '-v', 'ON_ERROR_STOP=1',
            '-c', 'CREATE EXTENSION IF NOT EXISTS pg_trgm')
        image = build['images']['backend']['local']
        password_hash = run('docker', 'run', '--rm', '--network', 'none', image, 'python', '-c',
                            'from argon2 import PasswordHasher; print(PasswordHasher().hash("ci-only"))')
        env = {'DATABASE_URL': f'postgresql+asyncpg://ci:ci-only@{names["db"]}:5432/ci',
               'REDIS_URL': f'redis://{names["redis"]}:6379/0', 'JWT_SECRET': 'isolated-ci-only-secret-32-characters',
               'SECRET_ENCRYPTION_KEY': 'MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=',
               'ADMIN_USERNAME': 'ci', 'ADMIN_PASSWORD_HASH': password_hash}
        env_args = [entry for key, value in env.items() for entry in ('-e', key + '=' + value)]
        run('docker', 'run', '-d', '--name', names['api'], '--network-alias', 'api', *common, *env_args,
            image, 'sh', '-ec', 'alembic upgrade head && exec uvicorn alphapulse.api.main:app --host 0.0.0.0')
        run('docker', 'run', '-d', '--name', names['web'], '--network-alias', 'web', *common, build['images']['web']['local'])
        wait_command('docker', 'exec', names['api'], 'python', '-c',
                     'import urllib.request; urllib.request.urlopen("http://localhost:8000/api/health", timeout=3)')
        print(run('docker', 'exec', '-i', names['api'], 'python', '-',
                  input=(ROOT / 'scripts/smoke_risk.py').read_text()), flush=True)
        print(run('docker', 'exec', '-i', names['api'], 'python', '-',
                  input=(ROOT / 'scripts/smoke_calendar.py').read_text()), flush=True)
        output = run('docker', 'exec', '-i', '-e', 'SMOKE_WEB=' + names['web'], names['api'], 'python', '-',
                     input=(ROOT / 'scripts/smoke_app.py').read_text())
        print(output, flush=True)
        # Force a different API address while retaining the synthetic database and Redis.
        old_address = json.loads(run('docker', 'inspect', names['api']))[0]['NetworkSettings']['Networks'][network]['IPAddress']
        run('docker', 'rm', '-f', names['api'])
        run('docker', 'run', '-d', '--name', network + '-address-holder', *common, 'redis:8-alpine', 'sleep', '180')
        run('docker', 'run', '-d', '--name', names['api'], '--network-alias', 'api', *common, *env_args,
            image, 'sh', '-ec', 'alembic upgrade head && exec uvicorn alphapulse.api.main:app --host 0.0.0.0')
        new_address = json.loads(run('docker', 'inspect', names['api']))[0]['NetworkSettings']['Networks'][network]['IPAddress']
        require(new_address != old_address, 'Acceptance did not exercise an API address change')
        wait_command('docker', 'exec', names['api'], 'python', '-c',
                     'import urllib.request; urllib.request.urlopen("http://web/api/health", timeout=3)')
        run('docker', 'exec', '-i', '-e', 'SMOKE_WEB=' + names['web'], names['api'], 'python', '-',
            input=(ROOT / 'scripts/smoke_app.py').read_text())
    except BaseException:
        for part in ('api', 'web'):
            result = subprocess.run(['docker', 'logs', '--tail', '30', names[part]], capture_output=True, text=True)
            print(result.stdout + result.stderr, flush=True)
        raise
    finally:
        cleanup(label)


def check():
    build = metadata()
    label = os.environ.get('CI_RESOURCE_ID', 'local-' + str(os.getpid()))
    require(re.fullmatch('[a-zA-Z0-9-]+', label), 'Invalid CI resource identifier')
    build['resource_id'] = label
    save(ROOT / 'build.json', build)
    for part in IMAGES:
        print('Checking ' + part, flush=True)
        run('docker', 'build', '--provenance=false', '--platform', 'linux/amd64', '--target', 'check',
            '-f', ROOT / f'deploy/Dockerfile.{part}', ROOT / part)
        reused = existing_image(part, build)
        local = reused or 'alphapulse-ci:' + label + '-' + part
        build['images'][part] = {'local': local, 'reused': bool(reused)}
        save(ROOT / 'build.json', build)
        if reused:
            run('docker', 'pull', reused)
        else:
            print('Building ' + part, flush=True)
            run('docker', 'build', '--provenance=false', '--platform', 'linux/amd64', '--target', 'runtime',
                '--build-arg', 'APP_VERSION=' + build['version'], '--build-arg', 'APP_COMMIT=' + build['commit'],
                '--label', f'{LABEL}={label}', '--tag', local, '-f', ROOT / f'deploy/Dockerfile.{part}', ROOT / part)
        info = json.loads(run('docker', 'image', 'inspect', local))[0]
        require(info['Architecture'] == 'amd64' and info['Os'] == 'linux', 'Unexpected image platform')
        require(info['Config']['Labels']['org.opencontainers.image.revision'] == build['commit'], 'Image commit mismatch')
    smoke(build, label)
    build['verified'] = True
    save(ROOT / 'build.json', build)
    print('All checks and isolated acceptance passed', flush=True)


def check_upload_layers(image):
    # Docker may send an entire compressed layer in one registry request.
    with tempfile.TemporaryDirectory(prefix='ap-layer-check-') as temporary:
        archive_path = Path(temporary) / 'image.tar'
        run('docker', 'save', '-o', archive_path, image)
        with tarfile.open(archive_path) as archive:
            manifest = json.load(archive.extractfile('manifest.json'))[0]
            for name in manifest['Layers']:
                with archive.extractfile(name) as stream:
                    first = stream.read(1024 * 1024)
                    compressor = None if first.startswith((b'\x1f\x8b', b'\x28\xb5\x2f\xfd')) else zlib.compressobj(wbits=31)
                    size = len(compressor.compress(first)) if compressor else len(first)
                    while chunk := stream.read(1024 * 1024):
                        size += len(compressor.compress(chunk)) if compressor else len(chunk)
                    if compressor:
                        size += len(compressor.flush())
                    require(size < 90_000_000, f'A compressed image layer ({size} bytes) exceeds the CDN upload budget')


def publish():
    require(os.environ.get('GITEA_EVENT_NAME') == 'push' and os.environ.get('GITEA_REF') == 'refs/heads/dev',
            'Only a dev push may publish development images')
    build = json.loads((ROOT / 'build.json').read_text())
    require(build.get('verified') and build['commit'] == os.environ['GITEA_SHA'], 'Missing acceptance for this commit')
    with locked(LOCK_DIR / '.publish.lock'):
        for part in IMAGES:
            current = existing_image(part, build)
            if not current:
                tag = IMAGES[part] + ':' + build['version']
                check_upload_layers(build['images'][part]['local'])
                run('docker', 'tag', build['images'][part]['local'], tag)
                run('docker', 'push', tag)
                current = existing_image(part, build)
                require(current, 'Published manifest is missing')
            build['images'][part]['digest'] = current
        save(ROOT / 'build.json', build)
    if output := os.environ.get('GITHUB_OUTPUT'):
        with open(output, 'a') as stream:
            stream.write(f'version={build["version"]}\ncommit={build["commit"]}\n')
            for part in IMAGES:
                stream.write(f'{part}={build["images"][part]["digest"]}\n')
    print('Verified development digests published', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('check', 'publish', 'cleanup'))
    args = parser.parse_args()
    if args.command == 'check':
        check()
    elif args.command == 'publish':
        publish()
    elif (ROOT / 'build.json').exists():
        build = json.loads((ROOT / 'build.json').read_text())
        cleanup(build['resource_id'], [v['local'] for v in build['images'].values()])


if __name__ == '__main__':
    main()
