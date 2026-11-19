"""Release fixed remote sources under a repository-wide persistent lock."""
import datetime
from collections import deque
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import time
import urllib.parse

from release import (GITEA_IMAGE, UPLOAD_LIMIT, client,
                     PLATFORMS, archive_name, digest, git_env, image_digest, registry_digest, registry_login,
                     require, run, verify_anonymous, write_json)

COMPLETE = 'release-complete.json'
INTERNAL = {'candidate.oci', 'packages.json'}

STATE = Path('/docker/gitea/mirrors/AlphaPulse')
CONTROL = Path(__file__).resolve().parents[1]


def ancestor(a,b):
    return subprocess.run(['git','merge-base','--is-ancestor',a,b],stdout=subprocess.DEVNULL).returncode == 0


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
    require(not same or candidates, 'Branch history no longer contains its successful baseline')
    return min(candidates, key=lambda r: (distances[r['source_sha']], -r['completed_at'], r['version']))


def release_notes(base, commit):
    if base is None: return 'init'
    groups={'feat':[],'fix':[],'other':[]}
    for line in run('git','log','--reverse','--format=%H%x09%s',base['source_sha']+'..'+commit).splitlines():
        sha,title=line.split('\t',1)
        category='feat' if re.match(r'^feat(?:\(|:|!)',title) else 'fix' if re.match(r'^fix(?:\(|:|!)',title) else 'other'
        groups[category].append(f'- {title} ([{sha[:8]}](https://git.134333.xyz/eezd/AlphaPulse/commit/{sha}))')
    body='\n\n'.join('### '+k+'\n\n'+'\n'.join(v) for k,v in groups.items() if v)
    previous=base['version']
    return body+f'\n\n[Gitea compare](https://git.134333.xyz/eezd/AlphaPulse/compare/{previous}...{commit})'


def verify_complete(api, record, *, require_marker):
    release = api.release(record['version'])
    require(release and not release['draft'] and not release['prerelease'], 'Release is not formally published')
    require(release['body'] == record['notes'] and release['name'] == 'AlphaPulse '+record['version'], 'Release metadata differs')
    require(api.repo('/commits/'+record['version'])['sha'] == record['source_sha'], 'Published tag differs')
    expected = set(record['assets'])
    names = [asset['name'] for asset in api.assets(release)]
    if require_marker or COMPLETE in names:
        expected.add(COMPLETE)
        marker = json.loads(api.asset_bytes(release, COMPLETE))
        require(marker == completion_marker(record), 'Release completion record differs')
    require(sorted(names) == sorted(expected), 'Published asset set differs')
    for name, checksum in record['assets'].items():
        require(hashlib.sha256(api.asset_bytes(release, name)).hexdigest() == checksum, 'Published asset differs: '+name)
    require(image_digest(GITEA_IMAGE+':'+record['version']) == record['image_digest'], 'Gitea image differs')
    return release


def completion_marker(record):
    return {'version': record['version'], 'source_sha': record['source_sha'],
            'build_sha256': record['assets']['build.json']}



def build_oci(image, destination, commit, version, packages_digest):
    builder = 'alphapulse-release-' + os.environ['CI_RUN_ID']
    require(re.fullmatch(r'alphapulse-release-[0-9]+', builder), 'Invalid builder identity')
    try:
        run('docker', 'buildx', 'create', '--name', builder, '--driver', 'docker-container',
            '--driver-opt', 'image=moby/buildkit@sha256:c5f8862f52499773af85bc07695b2379ef08e8002352b0df2c4864c91d78dabe', '--bootstrap')
        run('docker','buildx','build','--builder',builder,'--platform=linux/amd64,linux/arm64','--provenance=false',
            '--output=type=oci,dest='+str(destination)+',name=alphapulse-release-check:'+version,
            '--build-arg','APP_VERSION='+version,'--build-arg','APP_COMMIT='+commit,
            '--label','org.opencontainers.image.revision='+commit,'--label','org.opencontainers.image.version='+version,
            '--label','io.alphapulse.packages.sha256='+packages_digest,'-f',str(CONTROL/'deploy/Dockerfile.release'),str(image))
    finally:
        subprocess.run(['docker', 'buildx', 'rm', builder], check=False)

def needs_recovery(record, records_dir, api):
    if record.get('complete'):
        return False
    # Missing phases are legacy records, not proof that no publication occurred.
    if record.get('phase') != 'building' or record.get('assets') or record.get('image_digest'):
        return True
    cache = records_dir/record['version']
    if any((cache/name).exists() for name in ('checkpoint.json', 'packages.json', 'build.json')):
        return True
    release = api.release(record['version'])
    if release and (not release['draft'] or api.assets(release)):
        return True
    return any(registry_digest(GITEA_IMAGE, tag) is not None
               for tag in ('candidate-'+record['source_sha'], record['version']))


def check_pending(records_dir, version, api):
    for path in records_dir.glob('*.json'):
        if path.stem != version and needs_recovery(json.loads(path.read_text()), records_dir, api):
            raise ValueError('Resolve the pending release before starting another version: '+path.stem)


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


def verify_checkpoint(cache, record):
    checkpoint = json.loads((cache/'checkpoint.json').read_text())
    require(checkpoint['source_sha'] == record['source_sha'] and checkpoint['version'] == record['version'],
            'Artifact checkpoint identity differs')
    assets = {*[archive_name(record['version'], p) for p in PLATFORMS], 'SHA256SUMS', 'packages.json', 'build.json'}
    require(set(checkpoint['files']) == assets | {'candidate.oci'}, 'Artifact checkpoint is incomplete')
    for name, expected in checkpoint['files'].items():
        require((cache/name).is_file() and digest(cache/name) == expected, 'Original artifact corrupt or missing: '+name)
    metadata = json.loads((cache/'build.json').read_text())
    require(all(metadata[key] == record[key] for key in ('source_sha', 'version', 'source_branch', 'previous_tag')),
            'Build metadata identity differs')
    return checkpoint


def prepare_artifacts(cache, record):
    """Persist all tested originals before the first registry or asset upload."""
    require(record.get('phase') == 'building', 'Original checkpoint is missing; restore it before retrying publication')
    commit, version = record['source_sha'], record['version']
    packages = cache/'packages.json'
    if not packages.exists():
        # Candidate subprocesses receive no publication tokens or Docker credentials.
        source = Path('source').resolve()
        env = {k:v for k,v in os.environ.items() if k in ('PATH', 'HOME', 'TMPDIR', 'HOSTNAME')}
        env.update(ALPHAPULSE_BUILD_VERSION=version, ALPHAPULSE_BUILD_SHA=commit, CI_RUN_ID=os.environ['CI_RUN_ID'])
        subprocess.run(['python3', str(CONTROL/'scripts/package.py')], cwd=source, env=env, check=True)
        import shutil
        # Copy only the expected originals; incomplete copies remain unconfirmed.
        metadata = json.loads((source/'downloads/build.json').read_text())
        expected = {archive_name(version, p) for p in PLATFORMS}
        require(metadata['commit'] == commit and metadata['version'] == version, 'Package build identity differs')
        require(set(metadata['files']) == expected, 'Expected all six platform archives')
        for name in [*sorted(expected), 'SHA256SUMS']:
            shutil.copy2(source/'downloads'/name, cache/name)
        write_json(packages, metadata)
    metadata = json.loads(packages.read_text())
    require(metadata['commit'] == commit and metadata['version'] == version, 'Cached build identity differs')
    require(set(metadata['files']) == {archive_name(version, p) for p in PLATFORMS}, 'Expected all six platform archives')
    for name, info in metadata['files'].items():
        require(digest(cache/name) == info['sha256'], 'Cached package corrupt or missing: '+name)
    oci = cache/'candidate.oci'
    if not oci.exists():
        image = cache/'image'
        for platform in ('linux/amd64', 'linux/arm64'):
            target = image/platform
            target.mkdir(parents=True, exist_ok=True)
            with tarfile.open(cache/archive_name(version, platform)) as archive:
                archive.extractall(target, filter='data')
        temporary = cache/'candidate.next.oci'
        build_oci(image, temporary, commit, version, digest(packages))
        oci_manifest(temporary)
        temporary.replace(oci)
    value, manifests = oci_manifest(oci)
    local = 'alphapulse-release-check:'+version
    try:
        run('docker', 'load', '--input', str(oci))
        subprocess.run(['python3', str(CONTROL/'scripts/smoke-release.py'), local], check=True)
    finally:
        subprocess.run(['docker', 'image', 'rm', local], check=False)
    platforms = {f'{item["platform"]["os"]}/{item["platform"]["architecture"]}': item['digest']
                 for item in manifests}
    metadata.update(schema=3, source_sha=commit, source_branch=record['source_branch'], previous_tag=record['previous_tag'],
                    packages=[{'name': name, 'sha256': info['sha256']} for name, info in sorted(metadata['files'].items())],
                    image={'ref': GITEA_IMAGE+':'+version, 'digest': value, 'platforms': platforms})
    metadata['validation'].update(linux_images='amd64 native and arm64 emulated build, import and browser launch',
                                 isolated_business='Linux amd64 API, browser UI, SSE, WebSocket, migration and queue tests; synthetic data, no model calls')
    write_json(cache/'build.json', metadata)
    files = [*metadata['files'], 'SHA256SUMS', 'packages.json', 'build.json', 'candidate.oci']
    write_json(cache/'checkpoint.json', {'source_sha':commit, 'version':version,
                                       'files':{name:digest(cache/name) for name in files}})


def execute():
    branch=os.environ['SOURCE_BRANCH']; commit=os.environ['SOURCE_SHA']
    require(re.fullmatch('[0-9a-f]{40}',commit), 'Expected full source SHA')
    require(os.environ.get('GITHUB_REF','refs/heads/main')=='refs/heads/main','Control workflow must run from main')
    gitea = client()
    require(gitea.request('/user')['login'] == 'eezd', 'Unexpected publishing identity')
    quoted=urllib.parse.quote(branch,safe='')
    head=gitea.repo('/branches/'+quoted)['commit']['id']
    run('git','fetch','--no-tags','origin','+refs/heads/*:refs/remotes/origin/*','+refs/tags/*:refs/tags/*')
    require(ancestor(commit,head),'Source SHA is no longer reachable from the requested branch')
    timestamp=int(run('git','show','-s','--format=%ct',commit))
    version=datetime.datetime.fromtimestamp(timestamp,datetime.timezone.utc).strftime('%Y%m%d')+'-'+commit[:8]
    records_dir=STATE/'releases'; records_dir.mkdir(exist_ok=True)
    record_path=records_dir/(version+'.json')
    if record_path.exists():
        record=json.loads(record_path.read_text())
        require(record['source_sha']==commit,'Version collision')
        require(record.get('release_schema') == 3, 'Legacy release record requires separate reconciliation')
        if record.get('complete'):
            registry_login()
            verify_complete(gitea, record, require_marker=True)
            print('Reused complete Gitea release', version)
            return
        require(record.get('phase') in ('building', 'publishing'), 'Invalid release phase')
    registry_login()
    check_pending(records_dir, version, gitea)
    if not record_path.exists():
        require(not gitea.release(version), 'Existing legacy version must not be overwritten')
        previous=select_baseline([json.loads(p.read_text()) for p in records_dir.glob('*.json')],branch,commit)
        record={'source_branch':branch,'source_sha':commit,'version':version,'previous_tag':previous['version'] if previous else None,'notes':release_notes(previous,commit),'complete':False,'phase':'building','release_schema':3}
        write_json(record_path,record)
    cache=records_dir/version; cache.mkdir(exist_ok=True)
    existing = gitea.repo('/commits/'+version, missing=True)
    require(existing is None or existing['sha'] == commit, 'Tag collision')
    if existing is None:
        run('git','-c','user.name=eezd','-c','user.email=eezd@outlook.com','tag','-a',version,commit,'-m','AlphaPulse '+version)
        run('git','push','origin','refs/tags/'+version,env=git_env(gitea))
    release = gitea.release(version)
    if not release:
        release = gitea.repo('/releases', method='POST', data={'tag_name': version, 'target_commitish': commit,
                             'name': 'AlphaPulse '+version, 'body': record['notes'], 'draft': True})
    require(release['body'] == record['notes'] and release['name'] == 'AlphaPulse '+version
            and not release['prerelease'], 'Release metadata conflict')
    if not (cache/'checkpoint.json').exists():
        require(record['phase'] == 'building', 'Original checkpoint is missing; restore it before retrying publication')
        require(release['draft'] and not gitea.assets(release), 'Uploaded originals exist without a checkpoint; restore the original cache before retrying')
        for tag in ('candidate-'+commit, version):
            require(registry_digest(GITEA_IMAGE, tag) is None, 'Uploaded image exists without a checkpoint; restore the original cache before retrying')
        prepare_artifacts(cache, record)
    checkpoint = verify_checkpoint(cache, record)
    metadata = json.loads((cache/'build.json').read_text())
    value = metadata['image']['digest']
    files = sorted(set(checkpoint['files']) - INTERNAL)
    assets = {name:checkpoint['files'][name] for name in files}
    if record['phase'] == 'publishing':
        require(record['assets'] == assets and record['image_digest'] == value, 'Publishing checkpoint differs from frozen record')
    record.update(phase='publishing', image_digest=value,
                  assets=assets)
    # The complete checkpoint and publishing phase both precede upload side effects.
    write_json(record_path, record)
    candidate = GITEA_IMAGE+':candidate-'+commit
    existing = registry_digest(GITEA_IMAGE, candidate.rsplit(':', 1)[1])
    require(existing in (None, value), 'Candidate image conflicts with the original checkpoint')
    if existing is None:
        run('skopeo', 'copy', '--all', '--preserve-digests', '--authfile', os.environ['DOCKER_CONFIG']+'/config.json',
            'oci-archive:'+str(cache/'candidate.oci'), 'docker://'+candidate)
    require(image_digest(candidate) == value, 'Candidate image differs')
    reference = GITEA_IMAGE+':'+version
    existing = registry_digest(GITEA_IMAGE, version)
    require(existing in (None, value), 'Immutable image version conflict')
    if existing is None:
        run('skopeo', 'copy', '--all', '--preserve-digests', '--authfile', os.environ['DOCKER_CONFIG']+'/config.json',
            'docker://'+candidate, 'docker://'+reference)
    verify_anonymous(reference)
    for name in files:
        gitea.upload(release, cache/name)
    if release['draft']:
        require(sorted(asset['name'] for asset in gitea.assets(release)) == files, 'Draft asset set differs')
        gitea.repo('/releases/'+str(release['id']), method='PATCH', data={'draft': False})
    published = verify_complete(gitea, record, require_marker=False)
    write_json(cache/COMPLETE, completion_marker(record))
    gitea.upload(published, cache/COMPLETE)
    record.update(complete=True, phase='complete', completed_at=int(time.time()))
    write_json(record_path,record)
    print('Published to Gitea',version)

if __name__=='__main__':
    STATE.mkdir(exist_ok=True)
    with (STATE/'.release.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        execute()
