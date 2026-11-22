"""Build fixed sources in a temporary workspace and publish only to Forgejo."""
import datetime
from collections import deque
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

from release import (FORGEJO_GIT_URL, FORGEJO_IMAGE, UPLOAD_LIMIT, client,
                     PLATFORMS, archive_name, digest, git_env, image_digest, registry_digest, registry_login,
                     require, run, verify_anonymous, write_json)

COMPLETE = 'release-complete.json'
VERSION = re.compile(r'\d{8}-[0-9a-f]{8}')
SHA = re.compile(r'[0-9a-f]{64}')
CONTROL = Path(__file__).resolve().parents[1]
TERMINAL = {'success', 'failure', 'cancelled', 'skipped'}
_spec = importlib.util.spec_from_file_location('release_resolver', CONTROL/'scripts/resolve-release.py')
resolver = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(resolver)
_spec = importlib.util.spec_from_file_location('release_candidate', CONTROL/'scripts/release-candidate.py')
candidate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(candidate)


def release_context():
    require(os.environ.get('CONTROL_REF') == 'refs/heads/main', 'Control workflow must run from main')
    context = json.loads(Path(os.environ['RELEASE_CONTEXT']).read_text())
    require(context['schema'] == 1 and context['control_sha'] == os.environ.get('CONTROL_SHA')
            and resolver.SHA.fullmatch(context['control_sha'])
            and type(context['release_run_id']) is int and context['release_run_id'] > 0
            and str(context['release_run_id']) == os.environ.get('CI_RUN_ID'), 'Release context Actions identity differs')
    require(resolver.SHA.fullmatch(context['source_sha']) and resolver.SHA.fullmatch(context['source_branch_head'])
            and VERSION.fullmatch(context['version']) and context['version'].endswith(context['source_sha'][:8]),
            'Release context source identity differs')
    inputs = resolver.normalize_inputs(context['release_inputs'])
    require(inputs == context['release_inputs'] and inputs['source_branch'] == context['source_branch']
            and (inputs['source_sha'] or context['source_branch_head']) == context['source_sha'],
            'Release context inputs differ')
    require(str(resolver.uuid.UUID(context['release_request_id'])) == context['release_request_id'], 'Invalid release request UUID')
    return context


def ancestor(a, b):
    result = subprocess.run(['git', 'merge-base', '--is-ancestor', a, b], stdout=subprocess.DEVNULL)
    if result.returncode != 1:
        result.check_returncode()
    return result.returncode == 0


def select_baseline(records, branch, commit):
    same = [r for r in records if r.get('complete') and r.get('source_branch') == branch]
    if not same:
        return None
    parents = {parts[0]: parts[1:] for line in run('git', 'rev-list', '--parents', commit).splitlines()
               if (parts := line.split())}
    distances = {commit: 0}
    pending = deque([commit])
    while pending:
        child = pending.popleft()
        for parent in parents[child]:
            if parent not in distances:
                distances[parent] = distances[child] + 1
                pending.append(parent)
    candidates = [r for r in same if r['source_sha'] in distances]
    require(candidates, 'Branch history no longer contains its successful baseline')
    return min(candidates, key=lambda r: (distances[r['source_sha']], -r['completed_at'], r['version']))


def release_notes(base, commit):
    if base is None:
        return 'init'
    groups = {'Features': [], 'Bug Fixes': [], 'Other': []}
    for line in run('git', 'log', '--reverse', '--format=%H%x09%s', base['source_sha']+'..'+commit).splitlines():
        sha, title = line.split('\t', 1)
        category = ('Features' if re.match(r'^feat(?:\(|:|!)', title) else
                    'Bug Fixes' if re.match(r'^fix(?:\(|:|!)', title) else 'Other')
        groups[category].append(f'- {title} ([{sha[:8]}](https://git.134333.xyz/eezd/AlphaPulse/commit/{sha}))')
    body = '\n\n'.join('### '+k+'\n\n'+'\n'.join(v) for k, v in groups.items() if v)
    return body+f'\n\n[Forgejo compare](https://git.134333.xyz/eezd/AlphaPulse/compare/{base["version"]}...{commit})'


def published_files(version):
    return {archive_name(version, platform) for platform in PLATFORMS} | {'SHA256SUMS', 'build.json'}


def completion_marker(record):
    return {'version': record['version'], 'source_sha': record['source_sha'],
            'build_sha256': record['assets']['build.json']}


def completed_record(api, release):
    names = [a['name'] for a in api.assets(release)]
    if COMPLETE not in names:
        return None
    require(not release['draft'] and not release['prerelease'] and VERSION.fullmatch(release['tag_name']),
            'Completion marker on a non-formal release')
    version = release['tag_name']
    require(sorted(names) == sorted(published_files(version) | {COMPLETE}), 'Published asset set differs')
    raw = api.asset_bytes(release, 'build.json')
    build = json.loads(raw)
    sha = build['source_sha']
    require(build['version'] == version and re.fullmatch('[0-9a-f]{40}', sha) and version.endswith(sha[:8])
            and build['source_branch'], 'Published build identity differs')
    marker = {'version': version, 'source_sha': sha, 'build_sha256': hashlib.sha256(raw).hexdigest()}
    require(json.loads(api.asset_bytes(release, COMPLETE)) == marker, 'Release completion record differs')
    require(api.repo('/git/commits/'+version)['sha'] == sha, 'Published tag differs')
    checksums = {p['name']: p['sha256'] for p in build['packages']}
    require(set(checksums) == published_files(version) - {'SHA256SUMS', 'build.json'}
            and len(build['packages']) == len(checksums) and all(SHA.fullmatch(v) for v in checksums.values()),
            'Published package checksums differ')
    return {'version': version, 'source_sha': sha, 'source_branch': build['source_branch'], 'complete': True,
            'completed_at': datetime.datetime.fromisoformat(release['published_at']).timestamp(), 'notes': release['body'],
            'assets': {**checksums, 'build.json': marker['build_sha256']},
            'image_digest': build['image']['digest'], 'image': build['image']}


def verify_completed_record(api, release, record):
    require(release['name'] == 'AlphaPulse '+record['version'], 'Release metadata differs')
    entries = [line.split('  ', 1) for line in api.asset_bytes(release, 'SHA256SUMS').decode().splitlines()]
    expected = {name: sha for name, sha in record['assets'].items() if name not in {'build.json', 'SHA256SUMS'}}
    require(len(entries) == len(expected) and dict((name, sha) for sha, name in entries) == expected,
            'Published checksum list differs')
    for name, sha in record['assets'].items():
        require(hashlib.sha256(api.asset_bytes(release, name)).hexdigest() == sha, 'Published asset differs: '+name)
    reference = FORGEJO_IMAGE+':'+record['version']
    require(image_digest(reference, record['image']) == record['image_digest'], 'Forgejo image differs')
    verify_anonymous(reference, record['image'])


def complete_history(api):
    for release in api.pages('/releases', repo=True):
        record = completed_record(api, release)
        if record is not None:
            yield record


def build_oci(image, destination, commit, version, packages_digest):
    builder = candidate.ensure_builder()
    run('docker', 'buildx', 'build', '--builder', builder, '--platform=linux/amd64,linux/arm64', '--provenance=false',
        '--output=type=oci,dest='+str(destination)+',name='+candidate.resource_names()['check'],
        '--build-arg', 'APP_VERSION='+version, '--build-arg', 'APP_COMMIT='+commit,
        '--label', 'org.opencontainers.image.revision='+commit, '--label', 'org.opencontainers.image.version='+version,
        '--label', 'io.alphapulse.packages.sha256='+packages_digest, '-f', str(CONTROL/'deploy/Dockerfile.release'), str(image))


def oci_manifest(path):
    with tarfile.open(path) as archive:
        require(all(m.size < UPLOAD_LIMIT for m in archive), 'OCI blob exceeds upload budget')
        index = json.load(archive.extractfile('index.json'))
        require(len(index['manifests']) == 1, 'Expected one OCI image index')
        descriptor = index['manifests'][0]
        raw = archive.extractfile('blobs/sha256/'+descriptor['digest'].removeprefix('sha256:')).read()
        require('sha256:'+hashlib.sha256(raw).hexdigest() == descriptor['digest'], 'OCI index digest differs')
        manifest = json.loads(raw)
        require({(m['platform']['os'], m['platform']['architecture']) for m in manifest['manifests']}
                == {('linux', 'amd64'), ('linux', 'arm64')}, 'Unexpected OCI image platforms')
        return descriptor['digest'], manifest['manifests']


def prepare_artifacts(cache, record):
    """Build afresh without exposing publication credentials to candidate processes."""
    commit, version = record['source_sha'], record['version']
    source = Path('source').resolve()
    context = release_context()
    downloads = cache/'downloads'
    downloads.mkdir()
    candidate.build_packages(source, CONTROL, commit, context['control_sha'], version, downloads)
    metadata = json.loads((downloads/'build.json').read_text())
    expected = {archive_name(version, p) for p in PLATFORMS}
    require(metadata['commit'] == commit and metadata['version'] == version, 'Package build identity differs')
    require(set(metadata['files']) == expected, 'Expected all six platform archives')
    for name in [*sorted(expected), 'SHA256SUMS']:
        shutil.copy2(downloads/name, cache/name)
    for name, info in metadata['files'].items():
        require(digest(cache/name) == info['sha256'], 'Package checksum differs: '+name)
    write_json(cache/'packages.json', metadata)
    image = cache/'image'
    for platform in ('linux/amd64', 'linux/arm64'):
        target = image/platform
        target.mkdir(parents=True)
        with tarfile.open(cache/archive_name(version, platform)) as archive:
            archive.extractall(target, filter='data')
    oci = cache/'candidate.oci'
    build_oci(image, oci, commit, version, digest(cache/'packages.json'))
    value, manifests = oci_manifest(oci)
    local = candidate.resource_names()['check']
    try:
        run('docker', 'load', '--input', str(oci))
        subprocess.run([sys.executable, str(CONTROL/'scripts/smoke-release.py'), local], check=True)
    finally:
        subprocess.run(['docker', 'image', 'rm', local], check=False)
    context = release_context()
    require(context['source_sha'] == commit and context['version'] == version
            and context['source_branch'] == record['source_branch'], 'Artifact release context differs')
    metadata.update(schema=5, source_sha=commit, source_branch=record['source_branch'], previous_tag=record['previous_tag'],
                    release_run_id=context['release_run_id'], release_request_id=context['release_request_id'],
                    release_inputs=context['release_inputs'], control_sha=context['control_sha'],
                    source_branch_head=context['source_branch_head'],
                    packages=[{'name': name, 'sha256': info['sha256']} for name, info in sorted(metadata['files'].items())],
                    image={'ref': FORGEJO_IMAGE+':'+version, 'digest': value,
                           'architecture_tags': {f'linux/{arch}': version+'-'+arch for arch in ('amd64', 'arm64')},
                           'platforms': {f'{m["platform"]["os"]}/{m["platform"]["architecture"]}': m['digest'] for m in manifests}})
    metadata['validation'].update(linux_images='amd64 native and arm64 emulated build, import and browser launch',
                                 isolated_business='Linux amd64 API, browser UI, SSE, WebSocket, migration and queue tests; synthetic data, no model calls')
    write_json(cache/'build.json', metadata)


def version_images(version):
    return {tag: registry_digest(FORGEJO_IMAGE, tag)
            for tag in (version, version+'-amd64', version+'-arm64')}


def incomplete_identity(api, release, build, images):
    # Download counters and API list ordering are not publication identity.
    fields = ('id', 'tag_name', 'name', 'target_commitish', 'body', 'draft', 'prerelease',
              'created_at', 'published_at')
    assets = [{key: asset.get(key) for key in ('id', 'uuid', 'name', 'size', 'created_at', 'browser_download_url')}
              for asset in api.assets(release)]
    return {'release': {key: release.get(key) for key in fields}, 'build': build,
            'assets': sorted(assets, key=lambda asset: asset['name']), 'images': images}


def check_incomplete(api, release, branch, commit, version):
    """Require run provenance and a terminal previous writer before exact cleanup."""
    require(not release['prerelease'] and release['name'] == 'AlphaPulse '+version
            and release['target_commitish'] == commit, 'Unproven incomplete release identity')
    names = [a['name'] for a in api.assets(release)]
    require(COMPLETE not in names and 'build.json' in names and len(names) == len(set(names))
            and set(names) <= published_files(version), 'Incomplete release requires manual reconciliation')
    build = json.loads(api.asset_bytes(release, 'build.json'))
    require(build['version'] == version and build['source_sha'] == commit and build['schema'] in (4, 5)
            and build['source_branch'] == branch and build['image']['ref'] == FORGEJO_IMAGE+':'+version,
            'Unproven incomplete build identity')
    run_id = build['release_run_id']
    require(type(run_id) is int and run_id > 0 and str(run_id) != os.environ['CI_RUN_ID'], 'Previous release run is still active')
    previous = api.repo('/actions/runs/'+str(run_id))
    require(previous['id'] == run_id and previous['status'] in TERMINAL
            and previous['trigger_event'] == 'workflow_dispatch' and previous['workflow_id'] == 'release.yml',
            'Previous release run has not terminated or is unproven')
    event = json.loads(previous['event_payload'])
    inputs = event['inputs']
    if build['schema'] == 4:
        require(inputs == {'source_sha': commit, 'source_branch': build['source_branch'],
                           'request_id': build['release_request_id']}, 'Previous release inputs differ')
    else:
        require(event.get('ref') == 'refs/heads/main' and resolver.SHA.fullmatch(build['control_sha'])
                and previous.get('commit_sha') == build['control_sha'], 'Previous control provenance differs')
        normalized = resolver.normalize_inputs(inputs)
        require(normalized == build['release_inputs'] and normalized['source_branch'] == branch
                and resolver.SHA.fullmatch(build['source_branch_head'])
                and (normalized['source_sha'] or build['source_branch_head']) == commit,
                'Previous release inputs differ')
        require(str(resolver.uuid.UUID(build['release_request_id'])) == build['release_request_id'],
                'Invalid previous release request UUID')
        require(ancestor(commit, build['source_branch_head']), 'Previous source branch history differs')
    values = version_images(version)
    expected = {version: build['image']['digest'],
                **{version+'-'+arch: build['image'].get('platforms', {}).get('linux/'+arch)
                   for arch in ('amd64', 'arm64')}}
    require(all(value is None or value == expected[tag] for tag, value in values.items()),
            'Unproven incomplete image identity')
    return build, values


def publish(api, cache, branch, commit, version):
    existing = api.repo('/git/commits/'+version, missing=True)
    require(existing is None or existing['sha'] == commit, 'Tag collision')
    release = api.release(version)
    registry_login()
    if release and (record := completed_record(api, release)) is not None:
        require(record['source_sha'] == commit, 'Version collision')
        verify_completed_record(api, release, record)
        print('Reused complete Forgejo release', version)
        return
    if release:
        require(existing is not None, 'Incomplete release tag is missing')
        build, images = check_incomplete(api, release, branch, commit, version)
        identity = incomplete_identity(api, release, build, images)
        previous_tag, notes = build['previous_tag'], release['body']
    else:
        require(all(value is None for value in version_images(version).values()), 'Unproven existing image version')
        previous = select_baseline(complete_history(api), branch, commit)
        previous_tag, notes = (previous['version'] if previous else None), release_notes(previous, commit)
    record = {'source_branch': branch, 'source_sha': commit, 'version': version,
              'previous_tag': previous_tag, 'notes': notes}
    # Do not remove failure evidence or write remotely until every build/check has passed.
    prepare_artifacts(cache, record)
    metadata = json.loads((cache/'build.json').read_text())
    files = published_files(version)
    record.update(image_digest=metadata['image']['digest'], image=metadata['image'],
                  assets={name: digest(cache/name) for name in files})
    if release:
        current = api.release(version)
        require(current is not None and current['id'] == release['id']
                and completed_record(api, current) is None, 'Release changed during build')
        build, values = check_incomplete(api, current, branch, commit, version)
        require(incomplete_identity(api, current, build, values) == identity, 'Incomplete identity changed during build')
        require(api.repo('/git/commits/'+version)['sha'] == commit, 'Retained tag changed during build')
        for tag, value in values.items():
            if value is not None:
                require(registry_digest(FORGEJO_IMAGE, tag) == value, 'Image changed before deletion: '+tag)
                api.request('/packages/eezd/container/alphapulse/'+tag, method='DELETE')
                require(registry_digest(FORGEJO_IMAGE, tag) is None, 'Image deletion was not confirmed')
        api.repo('/releases/'+str(release['id']), method='DELETE')
        require(api.release(version) is None, 'Release deletion was not confirmed')
        require(api.repo('/git/commits/'+version)['sha'] == commit, 'Cleanup changed the retained tag')
    require(all(value is None for value in version_images(version).values()), 'Image appeared during build')
    if existing is None:
        run('git', '-c', 'user.name=eezd', '-c', 'user.email=eezd@outlook.com', 'tag', '-a', version, commit, '-m', 'AlphaPulse '+version)
        run('git', 'push', FORGEJO_GIT_URL, 'refs/tags/'+version, env=git_env(api))
    release = api.repo('/releases', method='POST', data={'tag_name': version, 'target_commitish': commit,
                       'name': 'AlphaPulse '+version, 'body': record['notes'], 'draft': True})
    require(release['draft'], 'Forgejo did not create a draft; reconcile before retrying')
    # Publish normal build metadata first; it also identifies an interrupted writer.
    api.upload(release, cache/'build.json')
    for name in sorted(files - {'build.json'}):
        api.upload(release, cache/name)
    reference = FORGEJO_IMAGE+':'+version
    require(all(value is None for value in version_images(version).values()), 'Image appeared during build')
    auth = os.environ['DOCKER_CONFIG']+'/config.json'
    for arch in ('amd64', 'arm64'):
        run('skopeo', '--override-os', 'linux', '--override-arch', arch, 'copy', '--preserve-digests',
            '--authfile', auth, 'oci-archive:'+str(cache/'candidate.oci'), 'docker://'+reference+'-'+arch)
    run('skopeo', 'copy', '--multi-arch=index-only', '--preserve-digests', '--authfile', auth,
        'oci-archive:'+str(cache/'candidate.oci'), 'docker://'+reference)
    require(image_digest(reference, record['image']) == record['image_digest'], 'Uploaded image differs')
    verify_anonymous(reference, record['image'])
    require(sorted(a['name'] for a in api.assets(release)) == sorted(files), 'Draft asset set differs')
    for name, checksum in record['assets'].items():
        require(hashlib.sha256(api.asset_bytes(release, name)).hexdigest() == checksum, 'Uploaded asset differs: '+name)
    require(api.repo('/git/commits/'+version)['sha'] == commit, 'Published tag differs')
    api.repo('/releases/'+str(release['id']), method='PATCH', data={'draft': False})
    published = api.release(version)
    require(not published['draft'] and not published['prerelease'] and published['body'] == record['notes']
            and published['name'] == 'AlphaPulse '+version, 'Release is not formally published')
    verify_completed_record(api, published, record)
    write_json(cache/COMPLETE, completion_marker(record))
    api.upload(published, cache/COMPLETE)
    require(completed_record(api, api.release(version)) is not None, 'Release completion verification failed')
    print('Published to Forgejo', version)


def promote_latest(api, branch):
    """Only advance the mutable OCI alias to the newest completed main ancestor."""
    if branch != 'main':
        return
    records = [record for record in complete_history(api) if record['source_branch'] == 'main']
    if not records:
        print('No completed main release; latest unchanged')
        return
    head = run('git', 'rev-parse', 'refs/remotes/origin/main')
    require(all(ancestor(record['source_sha'], head) for record in records),
            'Completed main release is outside current main history')
    candidates = [record for record in records
                  if all(ancestor(other['source_sha'], record['source_sha']) for other in records)]
    require(len(candidates) == 1, 'Completed main releases do not have a unique newest ancestor')
    latest = candidates[0]
    current = registry_digest(FORGEJO_IMAGE, 'latest')
    require(current is None or any(record['image_digest'] == current for record in records),
            'Existing latest is not a proven completed main image')
    verify_completed_record(api, api.release(latest['version']), latest)
    if current != latest['image_digest']:
        run('skopeo', 'copy', '--multi-arch=index-only', '--preserve-digests', '--authfile', os.environ['DOCKER_CONFIG']+'/config.json',
            'docker://'+FORGEJO_IMAGE+'@'+latest['image_digest'], 'docker://'+FORGEJO_IMAGE+':latest')
    require(image_digest(FORGEJO_IMAGE+':latest') == latest['image_digest'], 'Latest image differs')
    verify_anonymous(FORGEJO_IMAGE+':latest', latest['image'])
    print('Verified Forgejo OCI latest ->', latest['version'])


def execute():
    context = release_context()
    branch, commit = context['source_branch'], context['source_sha']
    require(run('git', '-C', 'source', 'rev-parse', 'HEAD') == commit, 'Candidate checkout differs from frozen SHA')
    api = client()
    require(api.request('/user')['login'] == 'eezd', 'Unexpected publishing identity')
    # An explicit fetch fails if the branch was deleted, even if a stale tracking ref exists.
    target = 'refs/remotes/origin/'+branch
    run('git', 'fetch', '--no-tags', FORGEJO_GIT_URL, '+refs/heads/'+branch+':'+target,
        '+refs/tags/*:refs/tags/*', env=git_env(api))
    head = run('git', 'rev-parse', '--verify', target)
    require(ancestor(commit, head), 'Source SHA is no longer reachable from the requested branch')
    timestamp = int(run('git', 'show', '-s', '--format=%ct', commit))
    version = datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc).strftime('%Y%m%d')+'-'+commit[:8]
    require(ancestor(commit, context['source_branch_head']), 'Frozen source is outside resolved branch history')
    require(version == context['version'], 'Frozen version differs from source commit date')
    print(json.dumps({'version': version, 'source_sha': commit, 'control_sha': context['control_sha'],
                      'release_run_id': context['release_run_id'], 'release_request_id': context['release_request_id']}), flush=True)
    with tempfile.TemporaryDirectory(prefix='alphapulse-release-') as temporary:
        publish(api, Path(temporary), branch, commit, version)
    promote_latest(api, branch)


if __name__ == '__main__':
    try:
        execute()
    finally:
        candidate.cleanup()
