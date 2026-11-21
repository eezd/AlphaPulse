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
        # 历史清理只在旧 schema 的独立数据库验证，不能触碰重新接入后的来源。
        for database, revision, script in (('cls_history_ci', 'c91234567890', 'smoke_remove_cls.py'),
                                           ('research_ci', 'head', 'smoke_research_sources.py')):
            run('docker', 'exec', names['db'], 'createdb', '-U', 'ci', database)
            run('docker', 'exec', names['db'], 'psql', '-U', 'ci', '-d', database,
                '-v', 'ON_ERROR_STOP=1', '-c', 'CREATE EXTENSION pg_trgm')
            database_url = env['DATABASE_URL'].rsplit('/', 1)[0] + '/' + database
            isolated = [*env_args, '-e', 'DATABASE_URL=' + database_url]
            run('docker', 'run', '--rm', *common, *isolated, image, 'alembic', 'upgrade', revision)
            if database == 'research_ci':
                extra = ['-e', 'RESEARCH_SOURCES_TEST_ISOLATED=1',
                         '-e', 'RESEARCH_SOURCES_TEST_DATABASE_URL=' + database_url,
                         '-e', 'RESEARCH_SOURCES_TEST_REDIS_URL=' + env['REDIS_URL'],
                         '-e', 'RESEARCH_SOURCES_TEST_FIXTURES=/research-fixtures']
                container = run('docker', 'create', '-i', *common, *isolated, *extra, image, 'python', '-')
                try:
                    run('docker', 'cp', str(ROOT / 'backend/tests/fixtures/research'), container + ':/research-fixtures')
                    print(run('docker', 'start', '-ai', container, input=(ROOT / 'scripts' / script).read_text()), flush=True)
                finally:
                    run('docker', 'rm', '-fv', container)
            else:
                print(run('docker', 'run', '--rm', '-i', *common, *isolated, image, 'python', '-',
                          input=(ROOT / 'scripts' / script).read_text()), flush=True)
        print(run('docker', 'run', '--rm', '-i', *common, *env_args, image, 'python', '-',
                  input=(ROOT / 'scripts/smoke_remove_trading.py').read_text()), flush=True)
        run('docker', 'run', '-d', '--name', names['api'], '--network-alias', 'api', *common, *env_args,
            image, 'sh', '-ec', 'alembic upgrade head && exec uvicorn alphapulse.api.main:app --host 0.0.0.0')
        wait_command('docker', 'exec', names['api'], 'python', '-c',
                     'import urllib.request; urllib.request.urlopen("http://localhost:8000/api/health", timeout=3)')
        print(run('docker', 'exec', '-i', '-e', 'AI_RECOVERY_TEST_REDIS_URL=' + env['REDIS_URL'],
                  names['api'], 'python', '-', input=(ROOT / 'scripts/smoke_ai_recovery.py').read_text()), flush=True)
        print(run('docker', 'exec', '-i', names['api'], 'python', '-',
                  input=(ROOT / 'scripts/smoke_remove_wscn_live.py').read_text()), flush=True)
        run('docker', 'cp', str(ROOT / 'scripts/maintenance.py'), names['api'] + ':/tmp/maintenance.py')
        run('docker', 'cp', str(ROOT / 'scripts/smoke_briefings.py'), names['api'] + ':/tmp/smoke_briefings.py')
        print(run('docker', 'exec', '-e', 'BRIEFINGS_TEST_DATABASE_URL=' + env['DATABASE_URL'],
                  '-e', 'BRIEFINGS_TEST_REDIS_URL=' + env['REDIS_URL'], '-e', 'BRIEFINGS_TEST_ISOLATED=1',
                  names['api'], 'python', '/tmp/smoke_briefings.py'), flush=True)
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
