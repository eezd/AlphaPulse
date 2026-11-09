"""Build six Python installation archives from a fixed, tested remote source."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import zipfile

ROOT = Path.cwd()
CONTROL = Path(__file__).resolve().parent
version = os.environ['ALPHAPULSE_BUILD_VERSION']
commit = os.environ['ALPHAPULSE_BUILD_SHA']
assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip() == commit
for command, directory in [(['uv', 'sync', '--locked', '--python', '3.13'], 'backend'),
                           (['uv', 'run', 'pytest', '-q'], 'backend'),
                           (['uvx', 'ruff==0.14.14', 'check', '--select', 'F,E9,B', 'src', 'tests'], 'backend'),
                           (['pnpm', 'install', '--frozen-lockfile'], 'web'), (['pnpm', 'lint:check'], 'web'),
                           (['pnpm', 'typecheck'], 'web'), (['pnpm', 'test'], 'web'), (['pnpm', 'build'], 'web')]:
    subprocess.run(command, cwd=ROOT/directory, check=True)
subprocess.run(['python3', '-m', 'unittest', 'discover', '-s', str(CONTROL/'tests'), '-q'], check=True)
(ROOT/'web/dist/version.json').write_text(json.dumps({'version': version, 'commit': commit}))
platforms = {'linux/amd64': 'x86_64-unknown-linux-gnu', 'linux/arm64': 'aarch64-unknown-linux-gnu',
             'darwin/amd64': 'x86_64-apple-darwin', 'darwin/arm64': 'aarch64-apple-darwin',
             'windows/amd64': 'x86_64-pc-windows-msvc', 'windows/arm64': 'aarch64-pc-windows-msvc'}
output = ROOT/'downloads'
output.mkdir(exist_ok=True)
files = {}
for target, triple in platforms.items():
    subprocess.run(['uv', 'sync', '--dry-run', '--locked', '--no-dev', '--no-install-project', '--python-platform', triple], cwd=ROOT/'backend', check=True)
    with tempfile.TemporaryDirectory() as temporary:
        stage = Path(temporary)
        shutil.copytree(ROOT/'backend', stage/'backend', ignore=shutil.ignore_patterns('.venv', '__pycache__', '.pytest_cache', '.ruff_cache'))
        shutil.copytree(ROOT/'web/dist', stage/'web')
        shutil.copy2(CONTROL/'install-package.py', stage/'install.py')
        for license_file in ROOT.glob('LICENSE*'):
            shutil.copy2(license_file, stage/license_file.name)
        (stage/'target.json').write_text(json.dumps({'platform': target, 'version': version, 'source_sha': commit}))
        (stage/'INSTALL.txt').write_text('Requires Python 3.13 and uv; external PostgreSQL and Redis; configure environment variables.\nRun python install.py install, then python install.py <role>.\nBrowser libraries must be installed separately on Linux. Windows arm64 may require Rust, MSVC and OpenSSL to build locked dependencies. No native macOS/Windows runtime validation was performed.\n')
        name = f'AlphaPulse_{version}_{target.replace("/", "_")}' + ('.zip' if target.startswith('windows') else '.tar.gz')
        if name.endswith('.zip'):
            with zipfile.ZipFile(output/name, 'w', zipfile.ZIP_DEFLATED) as archive:
                for p in sorted(stage.rglob('*')):
                    if p.is_file(): archive.write(p, p.relative_to(stage))
        else:
            with tarfile.open(output/name, 'w:gz') as archive:
                for p in sorted(stage.iterdir()): archive.add(p, arcname=p.name)
        size = (output/name).stat().st_size
        assert 0 < size < 90_000_000
        files[name] = {'sha256': hashlib.sha256((output/name).read_bytes()).hexdigest(), 'size': size}
(output/'SHA256SUMS').write_text(''.join(f'{v["sha256"]}  {k}\n' for k, v in files.items()))
(output/'build.json').write_text(json.dumps({'commit': commit, 'version': version, 'files': files,
    'platforms': list(platforms), 'validation': {'all_targets': 'locked dependency dry-run and installation archive', 'native_runtime': [], 'installation_requires_network': True}}, indent=2))
