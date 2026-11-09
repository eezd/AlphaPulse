"""Exercise release image with synthetic PostgreSQL/Redis on an internal network."""
import os
from pathlib import Path
import subprocess
import sys
import time
from common import run
ROOT = Path(__file__).resolve().parents[1]
LABEL = 'io.alphapulse.release.smoke'

def wait_command(*command, attempts=60):
    for attempt in range(attempts):
        try:
            return run(*command)
        except RuntimeError:
            if attempt == attempts - 1:
                raise
            time.sleep(1)


def cleanup(label):
    for identifier in run('docker', 'ps', '-aq', '--filter', f'label={LABEL}={label}').split():
        run('docker', 'rm', '-fv', identifier)
    for identifier in run('docker', 'network', 'ls', '-q', '--filter', f'label={LABEL}={label}').split():
        run('docker', 'network', 'rm', identifier)


def smoke(build, label):
    network = 'ap-ci-' + label
    names = {part: network + '-' + part for part in ('db', 'redis', 'api')}
    # Pull before entering the internal network; no runtime service can reach external I/O.
    for image in ('postgres:18-alpine', 'redis:8-alpine'):
        run('docker', 'pull', image)
    run('docker', 'network', 'create', '--internal', '--label', f'{LABEL}={label}', network)
    common = ['--network', network, '--label', f'{LABEL}={label}']
    try:
        run('docker', 'run', '-d', '--name', names['db'], *common, '-e', 'POSTGRES_PASSWORD=ci-only',
            '-e', 'POSTGRES_USER=ci', '-e', 'POSTGRES_DB=ci', 'postgres:18-alpine')
        run('docker', 'run', '-d', '--name', names['redis'], *common, 'redis:8-alpine')
        # The initialization server accepts sockets before the CI database exists.
        wait_command('docker', 'exec', names['db'], 'pg_isready', '-h', '127.0.0.1', '-U', 'ci', '-d', 'ci')
        run('docker', 'exec', names['db'], 'psql', '-h', '127.0.0.1', '-U', 'ci', '-d', 'ci', '-v', 'ON_ERROR_STOP=1',
            '-c', 'CREATE EXTENSION IF NOT EXISTS pg_trgm')
        image = build['image']
        password_hash = run('docker', 'run', '--rm', '--network', 'none', image, 'python', '-c',
                            'from argon2 import PasswordHasher; print(PasswordHasher().hash("ci-only"))')
        env = {'DATABASE_URL': f'postgresql+asyncpg://ci:ci-only@{names["db"]}:5432/ci',
               'REDIS_URL': f'redis://{names["redis"]}:6379/0', 'JWT_SECRET': 'isolated-ci-only-secret-32-characters',
               'SECRET_ENCRYPTION_KEY': 'MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=',
               'ADMIN_USERNAME': 'ci', 'ADMIN_PASSWORD_HASH': password_hash}
        env_args = [entry for key, value in env.items() for entry in ('-e', key + '=' + value)]
        print(run('docker', 'run', '--rm', '-i', *common, *env_args, image, 'python', '-',
                  input=(ROOT / 'scripts/smoke_remove_trading.py').read_text()), flush=True)
        run('docker', 'run', '-d', '--name', names['api'], '--network-alias', 'api', *common, *env_args,
            image, 'sh', '-ec', 'alembic upgrade head && exec uvicorn alphapulse.api.main:app --host 0.0.0.0')
        wait_command('docker', 'exec', names['api'], 'python', '-c',
                     'import urllib.request; urllib.request.urlopen("http://localhost:8000/api/health", timeout=3)')
        print(run('docker', 'exec', '-i', '-e', 'AI_RECOVERY_TEST_REDIS_URL=' + env['REDIS_URL'],
                  names['api'], 'python', '-', input=(ROOT / 'scripts/smoke_ai_recovery.py').read_text()), flush=True)
        print(run('docker', 'exec', '-i', names['api'], 'python', '-',
                  input=(ROOT / 'scripts/smoke_dynamic_queue.py').read_text()), flush=True)
        print(run('docker', 'exec', '-i', names['api'], 'python', '-',
                  input=(ROOT / 'scripts/smoke_priority_queue.py').read_text()), flush=True)
        print(run('docker', 'exec', '-i', names['api'], 'python', '-',
                  input=(ROOT / 'scripts/smoke_news_research.py').read_text()), flush=True)
        print(run('docker', 'exec', '-i', names['api'], 'python', '-',
                  input=(ROOT / 'scripts/smoke_calendar.py').read_text()), flush=True)
        print(run('docker', 'exec', '-i', names['api'], 'python', '-',
                  input=(ROOT / 'scripts/smoke_remove_cls.py').read_text()), flush=True)
        output = run('docker', 'exec', '-i', '-e', 'SMOKE_WEB=' + names['api'] + ':8000', names['api'], 'python', '-',
                     input=(ROOT / 'scripts/smoke_app.py').read_text())
        print(output, flush=True)
        try:
            print(run('docker', 'exec', '-i', '-e', 'SMOKE_WEB=' + names['api'] + ':8000', names['api'], 'python', '-',
                      input=(ROOT / 'scripts/smoke_workspace.py').read_text()), flush=True)
        finally:
            if destination := os.environ.get('CI_UI_SCREENSHOTS'):
                Path(destination).mkdir(parents=True, exist_ok=True)
                run('docker', 'cp', names['api'] + ':/tmp/alphapulse-ui/.', destination)
    except BaseException:
        result = subprocess.run(['docker', 'logs', '--tail', '30', names['api']], capture_output=True, text=True)
        print(result.stdout + result.stderr, flush=True)
        raise
    finally:
        cleanup(label)

if __name__ == '__main__':
    smoke({'image': sys.argv[1]}, 'release-' + str(os.getpid()))
