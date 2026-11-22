"""Secret-free candidate execution and exact per-attempt release resource cleanup."""
import argparse
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tempfile

from release import PLATFORMS, UPLOAD_LIMIT, archive_name, require, run

ROOT = Path(__file__).resolve().parents[1]
LABEL = 'io.alphapulse.ci.run'
BUILDKIT = 'moby/buildkit@sha256:c5f8862f52499773af85bc07695b2379ef08e8002352b0df2c4864c91d78dabe'


def identity():
    values = [os.environ.get(key, '') for key in ('CI_RUN_ID', 'CI_RUN_ATTEMPT')]
    require(all(re.fullmatch('[1-9][0-9]*', value) for value in values), 'Invalid release run/attempt identity')
    return 'release-'+'-'.join(values)


def resource_names():
    prefix = 'alphapulse-'+identity()
    return {'container': prefix+'-candidate', 'builder': prefix, 'context': prefix+'-context',
            'tools': prefix+'-tools:check', 'check': prefix+'-check:check'}


def ensure_builder():
    names = resource_names()
    contexts = run('docker', 'context', 'ls', '--format', '{{.Name}}').splitlines()
    if names['context'] not in contexts:
        run('docker', 'context', 'create', names['context'])
    builders = run('docker', 'buildx', 'ls', '--format', '{{.Name}}').splitlines()
    if names['builder'] not in builders:
        run('docker', 'buildx', 'create', '--name', names['builder'], '--driver', 'docker-container',
            '--driver-opt', 'image='+BUILDKIT, '--bootstrap', names['context'])
    return names['builder']


def cleanup():
    """Only remove the current attempt's reserved resources, never shared caches."""
    names = resource_names()
    selector = f'label={LABEL}={identity()}'
    for identifier in run('docker', 'ps', '-aq', '--filter', selector).split():
        run('docker', 'rm', '-fv', identifier)
    for identifier in run('docker', 'network', 'ls', '-q', '--filter', selector).split():
        run('docker', 'network', 'rm', identifier)
    for identifier in run('docker', 'volume', 'ls', '-q', '--filter', selector).split():
        run('docker', 'volume', 'rm', identifier)
    if names['builder'] in run('docker', 'buildx', 'ls', '--format', '{{.Name}}').splitlines():
        run('docker', 'buildx', 'rm', names['builder'])
    if names['context'] in run('docker', 'context', 'ls', '--format', '{{.Name}}').splitlines():
        run('docker', 'context', 'rm', names['context'])
    for reference in (names['tools'], names['check']):
        if run('docker', 'image', 'ls', '-q', '--filter', 'reference='+reference):
            run('docker', 'image', 'rm', reference)


def export_tree(repository, commit, destination, *, candidate):
    require(re.fullmatch('[0-9a-f]{40}', commit), 'Invalid exported Git identity')
    require(run('git', '-C', repository, 'rev-parse', 'HEAD') == commit, 'Export checkout differs from frozen SHA')
    with tempfile.TemporaryFile() as raw:
        subprocess.run(['git', '-C', str(repository), 'archive', '--format=tar', commit], stdout=raw, check=True)
        raw.seek(0)
        with tarfile.open(fileobj=raw) as source, tarfile.open(destination, 'w') as target:
            for member in source:
                require(not PurePosixPath(member.name).is_absolute() and '..' not in PurePosixPath(member.name).parts,
                        'Unsafe Git export path')
                member.uid = member.gid = 10001 if candidate else 0
                member.uname = member.gname = 'candidate' if candidate else 'root'
                executable = member.isdir() or member.mode & 0o111
                member.mode = (0o755 if executable else 0o644) if candidate else (0o555 if executable else 0o444)
                target.addfile(member, source.extractfile(member) if member.isfile() else None)


def copy_tree(container, archive, destination):
    with Path(archive).open('rb') as stream:
        subprocess.run(['docker', 'cp', '-a', '-', container+':'+destination], stdin=stream, check=True)


def receive_artifacts(container, version, destination):
    """Read a bounded flat regular-file set; never extract candidate links on the controller."""
    expected = {archive_name(version, platform) for platform in PLATFORMS} | {'SHA256SUMS', 'build.json'}
    seen = set()
    process = subprocess.Popen(['docker', 'cp', container+':/work/downloads', '-'], stdout=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=process.stdout, mode='r|') as archive:
            for member in archive:
                path = PurePosixPath(member.name)
                if member.isdir() and path.parts == ('downloads',):
                    continue
                require(member.isfile() and len(path.parts) == 2 and path.parts[0] == 'downloads'
                        and path.name in expected and path.name not in seen and 0 < member.size < UPLOAD_LIMIT,
                        'Unexpected candidate artifact: '+member.name)
                seen.add(path.name)
                with archive.extractfile(member) as source, (Path(destination)/path.name).open('xb') as target:
                    while chunk := source.read(1024*1024):
                        target.write(chunk)
        require(process.wait() == 0, 'Candidate artifact transfer failed')
        require(seen == expected, 'Candidate artifact set differs')
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.terminate()
            process.wait()


def build_packages(source, control, commit, control_sha, version, destination):
    names = resource_names()
    require(re.fullmatch(r'\d{8}-[0-9a-f]{8}', version) and version.endswith(commit[:8]), 'Invalid candidate version')
    with tempfile.TemporaryDirectory(prefix='alphapulse-candidate-') as directory:
        temporary = Path(directory)
        export_tree(source, commit, temporary/'source.tar', candidate=True)
        export_tree(control, control_sha, temporary/'control.tar', candidate=False)
        context = temporary/'context'
        context.mkdir()
        # The build context contains only a trusted Dockerfile, never the Runner workspace.
        with tarfile.open(temporary/'control.tar') as tree:
            dockerfile = tree.getmember('deploy/Dockerfile.release-candidate')
            require(dockerfile.isfile(), 'Candidate Dockerfile must be a regular trusted file')
            (context/'Dockerfile').write_bytes(tree.extractfile(dockerfile).read())
        builder = ensure_builder()
        run('docker', 'buildx', 'build', '--builder', builder, '--load', '--tag', names['tools'],
            '--label', LABEL+'='+identity(), str(context))
        command = ['docker', 'create', '--name', names['container'], '--label', LABEL+'='+identity(),
                   '--user', '10001:10001', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                   '--workdir', '/work', '--env', 'HOME=/home/candidate', '--env', 'TMPDIR=/tmp',
                   '--env', 'ALPHAPULSE_BUILD_SHA='+commit, '--env', 'ALPHAPULSE_BUILD_VERSION='+version,
                   names['tools'], 'uv', 'run', '--no-project', '--no-python-downloads',
                   '--python', '3.13', 'python', '/control/scripts/package.py']
        run(*command)
        copy_tree(names['container'], temporary/'source.tar', '/work')
        copy_tree(names['container'], temporary/'control.tar', '/control')
        # Attach without capturing the full test log in memory. The container has its own PID namespace.
        subprocess.run(['docker', 'start', '-ai', names['container']], check=True)
        receive_artifacts(names['container'], version, destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cleanup', action='store_true', required=True)
    parser.parse_args()
    cleanup()


if __name__ == '__main__':
    main()
