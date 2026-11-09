"""Build isolated native candidates; change running services only after checks pass."""
import datetime
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
import urllib.request

from common import save

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'runtime'
ROLES = {'api': 'alphapulse.api.main:app', 'crawler': 'alphapulse.crawler.manager',
         'market': 'alphapulse.market.worker', 'calendar': 'alphapulse.calendar.worker',
         'news-risk': 'alphapulse.news_research.worker', 'jin10-calendar': 'alphapulse.calendar.jin10_worker'}
UNITS = ['alphapulse-'+role+'.service' for role in ROLES]


def run(*args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


def identity():
    return {'source_sha': run('git', '-C', str(ROOT), 'rev-parse', 'HEAD'),
            'dirty': bool(run('git', '-C', str(ROOT), 'status', '--porcelain'))}


def build():
    before = identity()
    stamp = int(run('git', '-C', str(ROOT), 'show', '-s', '--format=%ct', before['source_sha']))
    version = datetime.datetime.fromtimestamp(stamp, datetime.UTC).strftime('%Y%m%d')+'-'+before['source_sha'][:8]
    candidate = RUNTIME/'builds'/(time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())+'-'+os.urandom(4).hex())
    candidate.mkdir(parents=True)
    for directory in ('backend', 'web'):
        shutil.copytree(ROOT/directory, candidate/directory, ignore=shutil.ignore_patterns('.venv', 'node_modules', 'dist', '__pycache__', '.pytest_cache', '.ruff_cache'))
    shutil.copytree(ROOT/'scripts', candidate/'scripts', ignore=shutil.ignore_patterns('__pycache__'))
    for script in ('start.sh', 'stop.sh', 'status.sh', 'release.sh'):
        shutil.copy2(ROOT/script, candidate/script)
    subprocess.run(['uv', 'sync', '--locked', '--python', '/usr/bin/python3.13'], cwd=candidate/'backend', check=True)
    subprocess.run(['uv', 'run', 'pytest', '-q'], cwd=candidate/'backend', check=True)
    subprocess.run(['uvx', 'ruff==0.14.14', 'check', '--select', 'F,E9,B', 'src', 'tests'], cwd=candidate/'backend', check=True)
    for args in (['pnpm', 'install', '--frozen-lockfile'], ['pnpm', 'lint:check'], ['pnpm', 'typecheck'], ['pnpm', 'test'], ['pnpm', 'build']):
        subprocess.run(args, cwd=candidate/'web', check=True)
    subprocess.run(['python3', '-m', 'unittest', 'discover', '-s', str(candidate/'scripts/tests'), '-q'], cwd=candidate, check=True)
    checked_scripts = ['common.py', 'mirror.py', 'native.py', 'maintenance.py', 'release.py',
                       'manual-release.py', 'package.py', 'install-package.py', 'smoke-release.py', 'tests']
    subprocess.run(['uvx', 'ruff==0.14.14', 'check', '--select', 'F,E9,B',
                    *(str(candidate/'scripts'/name) for name in checked_scripts)], check=True)
    (candidate/'web/dist/version.json').write_text(json.dumps({'version': version, 'commit': before['source_sha']}))
    metadata = {**before, 'version': version, 'dirty_end': identity()['dirty'], 'source_sha_end': identity()['source_sha'],
                'built_at': datetime.datetime.now(datetime.UTC).isoformat()}
    (candidate/'build.json').write_text(json.dumps(metadata, indent=2)+'\n')
    # Service interpreter must be accessible outside /root, with project files installed non-editably.
    subprocess.run(['uv', 'sync', '--locked', '--no-dev', '--no-editable', '--python', '/usr/bin/python3.13'], cwd=candidate/'backend', check=True)
    subprocess.run([str(candidate/'backend/.venv/bin/python'), '-m', 'playwright', 'install', '--only-shell', 'chromium'],
                   env={**os.environ, 'PLAYWRIGHT_BROWSERS_PATH': str(RUNTIME/'data/ms-playwright')}, check=True)
    return candidate


def unit_text(role):
    python = str(RUNTIME/'current/backend/.venv/bin/python')
    command = (python+' -m uvicorn '+ROLES[role]+' --host 127.0.0.1 --port 21005 --proxy-headers --forwarded-allow-ips 127.0.0.1'
               if role == 'api' else python+' -m '+ROLES[role])
    return f'''[Unit]
Description=AlphaPulse {role}
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=alphapulse
Group=alphapulse
WorkingDirectory={RUNTIME}/current/backend
EnvironmentFile={RUNTIME}/config/app.env
Environment=PLAYWRIGHT_BROWSERS_PATH={RUNTIME}/data/ms-playwright
Environment=HOME={RUNTIME}/data
Environment=PYTHONUNBUFFERED=1
ExecStart={command}
Restart=on-failure
RestartSec=5
TimeoutStopSec={3600 if role in ('api', 'news-risk') else 120}
KillMode=control-group
UMask=0027
[Install]
WantedBy=multi-user.target
'''


def healthy(candidate, roles=tuple(ROLES)):
    metadata = json.loads((candidate/'build.json').read_text())
    for _ in range(60):
        try:
            for role in roles:
                run('systemctl', 'is-active', '--quiet', 'alphapulse-'+role+'.service')
            for path in (('/api/health', '/version.json') if 'api' in roles else ()):
                with urllib.request.urlopen('http://127.0.0.1:21005'+path, timeout=3) as response:
                    body = json.load(response)
                assert (body.get('data') or body)['commit'] == metadata['source_sha']
            return
        except (OSError, ValueError, AssertionError, subprocess.CalledProcessError):
            time.sleep(1)
    raise RuntimeError('Candidate services or served version failed health verification')


def link(target):
    temporary = RUNTIME/'current.next'
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    temporary.replace(RUNTIME/'current')


def activate(candidate, before_workers=None):
    unit_dir = Path('/etc/systemd/system')
    previous = (RUNTIME/'current').resolve() if (RUNTIME/'current').exists() else None
    originals = {u: (unit_dir/u).read_bytes() if (unit_dir/u).exists() else None for u in UNITS}
    active = [u for u in UNITS if subprocess.run(['systemctl', 'is-active', '--quiet', u], capture_output=True).returncode == 0]
    enabled = [u for u in UNITS if subprocess.run(['systemctl', 'is-enabled', '--quiet', u], capture_output=True).returncode == 0]
    # The environment belongs to this build; persistent secrets remain in a separate file.
    meta = json.loads((candidate/'build.json').read_text())
    try:
        link(candidate)
        for role in ROLES:
            text = unit_text(role).replace('ExecStart=', f'Environment=APP_VERSION={meta["version"]}\nEnvironment=APP_COMMIT={meta["source_sha"]}\nEnvironment=WEB_DIST_DIR={candidate}/web/dist\nExecStart=')
            (unit_dir/('alphapulse-'+role+'.service')).write_text(text)
        run('systemctl', 'daemon-reload')
        run('systemctl', 'enable', *UNITS)
        run('systemctl', 'restart', UNITS[0])
        healthy(candidate, ('api',))
        if before_workers:
            before_workers()
        run('systemctl', 'restart', *UNITS[1:])
        healthy(candidate)
    except BaseException:
        subprocess.run(['systemctl', 'stop', *UNITS], check=True)
        for unit in UNITS:
            if unit not in enabled:
                run('systemctl', 'disable', unit)
        if previous:
            link(previous)
        else:
            (RUNTIME/'current').unlink(missing_ok=True)
        for unit, data in originals.items():
            if data is None:
                (unit_dir/unit).unlink(missing_ok=True)
            else:
                (unit_dir/unit).write_bytes(data)
        run('systemctl', 'daemon-reload')
        if active:
            run('systemctl', 'start', *active)
            healthy(previous, tuple(u.removeprefix('alphapulse-').removesuffix('.service') for u in active))
        raise


def maintenance(candidate, action, base='http://127.0.0.1:21005'):
    subprocess.run([str(candidate/'backend/.venv/bin/python'), str(ROOT/'scripts/maintenance.py'), action,
                    str(RUNTIME/'config/app.env'), str(RUNTIME/'deployment.pending.json'), base],
                   cwd=candidate/'backend', check=True)


def update(candidate):
    pending = RUNTIME/'deployment.pending.json'
    if pending.exists():
        raise RuntimeError('Pending maintenance exists; inspect runtime/deployment.pending.json before proceeding')
    maintenance(candidate, 'schema')
    previous = (RUNTIME/'current').resolve() if (RUNTIME/'current').exists() else None
    if previous:
        maintenance(previous, 'schema')
    save(pending, {'operation': 'native-update', 'candidate': str(candidate), 'previous': str(previous), 'phase': 'draining'})
    try:
        maintenance(candidate, 'pause')
        if previous:
            maintenance(candidate, 'drain')
        maintenance(candidate, 'assert-paused')
        activate(candidate)
        maintenance(candidate, 'restore')
    except BaseException:
        # activate restores the prior service state; never resume AI on an unhealthy version.
        if previous:
            healthy(previous)
            maintenance(previous, 'restore')
        raise
    else:
        pending.unlink()


def main():
    if os.geteuid() != 0:
        raise RuntimeError('Run as root to manage AlphaPulse systemd services')
    if not (RUNTIME/'config/app.env').is_file():
        raise RuntimeError('Missing runtime/config/app.env; initialize native configuration first')
    run('id', 'alphapulse')
    with (RUNTIME/'.deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (RUNTIME/'deployment.pending.json').exists():
            raise RuntimeError('Pending maintenance exists; inspect runtime/deployment.pending.json first')
        candidate = build()
        # First Docker-to-native cutover is a separately locked, explicit operation.
        if not (RUNTIME/'legacy-retired.json').exists() and shutil.which('docker') and subprocess.check_output(['docker', 'ps', '-q', '--filter', 'label=com.docker.compose.project=alphapulse-dev']).strip():
            print('Candidate prepared:', candidate)
            raise RuntimeError('Existing Docker instance is active; use the authorized migration procedure')
        update(candidate)
        print('Started:', candidate)


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    main()
