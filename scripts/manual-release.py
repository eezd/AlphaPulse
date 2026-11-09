"""Release fixed remote sources under a repository-wide persistent lock."""
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import urllib.parse

from release import (GITEA_IMAGE, GITHUB_IMAGE, UPLOAD_LIMIT, clients,
                     PLATFORMS, archive_name, digest, git_env, image_digest, registry_digest, registry_login,
                     require, run, verify_anonymous, write_json)
from mirror import synchronize

STATE = Path('/docker/gitea/mirrors/AlphaPulse')
CONTROL = Path(__file__).resolve().parents[1]


def ancestor(a,b):
    return subprocess.run(['git','merge-base','--is-ancestor',a,b],stdout=subprocess.DEVNULL).returncode == 0


def select_baseline(records, branch, commit):
    same = [r for r in records if r.get('complete') and r.get('source_branch') == branch]
    candidates = [r for r in same if ancestor(r['source_sha'],commit)]
    require(not same or candidates, 'Branch history no longer contains its successful baseline')
    return min(candidates,key=lambda r:(int(run('git','rev-list','--count',r['source_sha']+'..'+commit)), -r['completed_at'],r['version'])) if candidates else None


def release_notes(base, commit):
    if base is None: return 'init'
    groups={'feat':[],'fix':[],'other':[]}
    for line in run('git','log','--reverse','--format=%H%x09%s',base['source_sha']+'..'+commit).splitlines():
        sha,title=line.split('\t',1)
        category='feat' if re.match(r'^feat(?:\(|:|!)',title) else 'fix' if re.match(r'^fix(?:\(|:|!)',title) else 'other'
        groups[category].append(f'- {title} ([{sha[:8]}](https://git.134333.xyz/eezd/AlphaPulse/commit/{sha}))')
    body='\n\n'.join('### '+k+'\n\n'+'\n'.join(v) for k,v in groups.items() if v)
    previous=base['version']
    return body+f'\n\n[Gitea compare](https://git.134333.xyz/eezd/AlphaPulse/compare/{previous}...{commit}) · [GitHub compare](https://github.com/eezd/AlphaPulse/compare/{previous}...{commit})'


def verify_complete(apis, record):
    for api in apis:
        release=api.release(record['version'])
        require(release and not release['draft'] and not release['prerelease'], 'Release is not formally published')
        require(release['body']==record['notes'] and release['name']=='AlphaPulse '+record['version'], 'Release metadata differs')
        require(api.repo('/commits/'+record['version'])['sha']==record['source_sha'], 'Published tag differs')
        for name, info in record['assets'].items():
            require(hashlib.sha256(api.asset_bytes(release,name)).hexdigest()==info, 'Published asset differs: '+name)
    require(image_digest(GITEA_IMAGE+':'+record['version'])==record['image_digest'], 'Gitea image differs')
    require(image_digest(GITHUB_IMAGE+':'+record['version'])==record['image_digest'], 'GHCR image differs')



def build_oci(image, destination, commit, version, packages_digest):
    builder = 'alphapulse-release-' + os.environ['CI_RUN_ID']
    require(re.fullmatch(r'alphapulse-release-[0-9]+', builder), 'Invalid builder identity')
    try:
        run('docker', 'buildx', 'create', '--name', builder, '--driver', 'docker-container',
            '--driver-opt', 'image=moby/buildkit@sha256:c5f8862f52499773af85bc07695b2379ef08e8002352b0df2c4864c91d78dabe', '--bootstrap')
        run('docker','buildx','build','--builder',builder,'--platform=linux/amd64,linux/arm64','--provenance=false',
            '--output=type=oci,dest='+str(destination), '--build-arg','APP_VERSION='+version,'--build-arg','APP_COMMIT='+commit,
            '--label','org.opencontainers.image.revision='+commit,'--label','org.opencontainers.image.version='+version,
            '--label','io.alphapulse.packages.sha256='+packages_digest,'-f',str(CONTROL/'deploy/Dockerfile.release'),str(image))
    finally:
        subprocess.run(['docker', 'buildx', 'rm', builder], check=False)

def execute():
    branch=os.environ['SOURCE_BRANCH']; commit=os.environ['SOURCE_SHA']
    require(re.fullmatch('[0-9a-f]{40}',commit), 'Expected full source SHA')
    require(os.environ.get('GITHUB_REF','refs/heads/main')=='refs/heads/main','Control workflow must run from main')
    apis=clients(); gitea,github=apis
    for api in apis: require(api.request('/user')['login']=='eezd','Unexpected publishing identity')
    require(github.repo()['permissions']['push'],'GitHub repository write permission required')
    scopes={s.strip() for s in github.headers.get('X-OAuth-Scopes','').split(',')}
    require({'write:packages','workflow'} <= scopes and bool(scopes & {'repo','public_repo'}),'GitHub classic PAT needs repository, workflow and packages access')
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
        if record.get('complete'):
            registry_login(); verify_complete(apis,record); print('Reused complete release',version); return
    else:
        require(not any(not json.loads(p.read_text()).get('complete') for p in records_dir.glob('*.json')),'Resolve the pending release before starting another version')
        require(not gitea.release(version) and not github.release(version),'Existing legacy version must not be overwritten')
        previous=select_baseline([json.loads(p.read_text()) for p in records_dir.glob('*.json')],branch,commit)
        record={'source_branch':branch,'source_sha':commit,'version':version,'previous_tag':previous['version'] if previous else None,'notes':release_notes(previous,commit),'complete':False}
        write_json(record_path,record)
    cache=records_dir/version; cache.mkdir(exist_ok=True)
    for api in apis:
        existing=api.repo('/commits/'+version,missing=True)
        require(existing is None or existing['sha']==commit,'Tag collision')
    if gitea.repo('/commits/'+version,missing=True) is None:
        run('git','-c','user.name=eezd','-c','user.email=eezd@outlook.com','tag','-a',version,commit,'-m','AlphaPulse '+version)
        run('git','push','origin','refs/tags/'+version,env=git_env(gitea))
    synchronize(STATE,'https://git.134333.xyz/eezd/AlphaPulse.git','https://github.com/eezd/AlphaPulse.git',git_env(github))
    releases=[]
    for api in apis:
        r=api.release(version)
        if not r:
            r=api.repo('/releases',method='POST',data={'tag_name':version,'target_commitish':commit,'name':'AlphaPulse '+version,'body':record['notes'],'draft':True})
        require(r['body']==record['notes'],'Release notes conflict')
        releases.append(r)
    packages=cache/'packages.json'
    if not packages.exists():
        require(not any(a['name']=='packages.json' for a in gitea.assets(releases[0])), 'Original cached build is missing; restore its original draft assets before retrying')
        # Candidate subprocesses receive no publication tokens or Docker credentials.
        source=Path('source').resolve()
        env={k:v for k,v in os.environ.items() if k in ('PATH','HOME','TMPDIR','HOSTNAME')}
        env.update(ALPHAPULSE_BUILD_VERSION=version, ALPHAPULSE_BUILD_SHA=commit, CI_RUN_ID=os.environ['CI_RUN_ID'])
        subprocess.run(['python3', str(CONTROL/'scripts/package.py')], cwd=source, env=env, check=True)
        import shutil
        for p in (source/'downloads').iterdir(): shutil.copy2(p,cache/p.name)
        metadata=json.loads((cache/'build.json').read_text())
        (cache/'build.json').unlink()
        write_json(packages,metadata)
    metadata=json.loads(packages.read_text())
    require(metadata['commit']==commit and metadata['version']==version,'Cached build identity differs')
    require(set(metadata['files']) == {archive_name(version, p) for p in PLATFORMS}, 'Expected all six platform archives')
    for name,info in metadata['files'].items():
        require(digest(cache/name)==info['sha256'],'Cached package corrupt or missing')
    for name in [*metadata['files'],'SHA256SUMS','packages.json']:
        gitea.upload(releases[0],cache/name)
    registry_login()
    candidate=GITEA_IMAGE+':candidate-'+commit
    if not record.get('image_digest'):
        existing=registry_digest(GITEA_IMAGE,candidate.rsplit(':',1)[1])
        if existing:
            run('docker','pull',candidate)
            labels=json.loads(run('docker','image','inspect',candidate))[0]['Config']['Labels']
            require(labels.get('io.alphapulse.packages.sha256')==digest(packages),'Existing candidate belongs to another package build')
            record['image_digest']=image_digest(candidate);write_json(record_path,record)
    if not record.get('image_digest'):
        image=cache/'image';image.mkdir(parents=True,exist_ok=True)
        for platform in ('linux/amd64','linux/arm64'):
            target=image/platform;target.mkdir(parents=True,exist_ok=True)
            name='AlphaPulse_'+version+'_'+platform.replace('/','_')+'.tar.gz'
            with tarfile.open(cache/name) as archive: archive.extractall(target,filter='data')
        oci=cache/'candidate.oci'
        build_oci(image, oci, commit, version, digest(packages))
        with tarfile.open(oci) as archive: require(all(m.size<UPLOAD_LIMIT for m in archive),'OCI blob exceeds upload budget')
        run('skopeo','copy','--all','--preserve-digests','--authfile',os.environ['DOCKER_CONFIG']+'/config.json','oci-archive:'+str(oci),'docker://'+candidate)
        record['image_digest']=image_digest(candidate);write_json(record_path,record)
    if not record.get('smoke_passed'):
        subprocess.run(['python3', str(CONTROL/'scripts/smoke-release.py'), candidate], check=True)
        record['smoke_passed'] = True
        write_json(record_path, record)
    value=record['image_digest']
    require(image_digest(candidate)==value,'Candidate image differs')
    manifest=json.loads(run('docker','buildx','imagetools','inspect',candidate,'--raw'))
    metadata.update(schema=2,source_sha=commit,source_branch=record['source_branch'],previous_tag=record['previous_tag'],
        images={'gitea':GITEA_IMAGE+':'+version,'ghcr':GITHUB_IMAGE+':'+version,'digest':value,'manifests':manifest['manifests']})
    metadata['validation'].update(linux_images='amd64 native and arm64 emulated build, import and browser launch',
                                  isolated_business='Linux amd64 API, browser UI, SSE, WebSocket, migration and queue tests; synthetic data, no model calls')
    write_json(cache/'build.json',metadata)
    for image in (GITEA_IMAGE,GITHUB_IMAGE):
        existing=registry_digest(image,version);require(existing in (None,value),'Immutable image version conflict')
        if not existing: run('skopeo','copy','--all','--preserve-digests','--authfile',os.environ['DOCKER_CONFIG']+'/config.json','docker://'+candidate,'docker://'+image+':'+version)
    verify_anonymous(GITHUB_IMAGE+':'+version)
    verify_anonymous(GITEA_IMAGE+':'+version)
    files=[*metadata['files'],'SHA256SUMS','packages.json','build.json']
    record['assets']={name:digest(cache/name) for name in files}
    write_json(record_path,record)
    for api,r in zip(apis,releases,strict=True):
        for name in files: api.upload(r,cache/name)
        if r['draft']:api.repo('/releases/'+str(r['id']),method='PATCH',data={'draft':False})
    verify_complete(apis,record)
    record.update(complete=True,completed_at=int(__import__('time').time()))
    write_json(record_path,record)
    print('Published',version)

if __name__=='__main__':
    STATE.mkdir(exist_ok=True)
    with (STATE/'.release.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        execute()
