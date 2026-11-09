"""Install a platform-labelled Python distribution, then run one explicit role."""
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

root = Path(__file__).resolve().parent
metadata = json.loads((root/'target.json').read_text())
system = {'Linux': 'linux', 'Darwin': 'darwin', 'Windows': 'windows'}[platform.system()]
arch = {'x86_64': 'amd64', 'AMD64': 'amd64', 'aarch64': 'arm64', 'ARM64': 'arm64', 'arm64': 'arm64'}[platform.machine()]
if metadata['platform'] != system+'/'+arch:
    raise SystemExit('Package platform does not match this machine')
roles = {'api': ['uvicorn', 'alphapulse.api.main:app', '--host', '127.0.0.1', '--port', '21005'],
         'crawler': ['alphapulse.crawler.manager'], 'market': ['alphapulse.market.worker'],
         'calendar': ['alphapulse.calendar.worker'], 'news-risk': ['alphapulse.news_research.worker'],
         'jin10-calendar': ['alphapulse.calendar.jin10_worker']}
if sys.argv[1:] == ['install']:
    subprocess.run(['uv', 'sync', '--locked', '--no-dev', '--no-editable', '--python', '3.13'], cwd=root/'backend', check=True)
    subprocess.run(['uv', 'run', '--no-sync', 'playwright', 'install', '--only-shell', 'chromium'], cwd=root/'backend', check=True)
elif len(sys.argv) == 2 and sys.argv[1] in roles:
    env = {**os.environ, 'WEB_DIST_DIR': str(root/'web'), 'APP_VERSION': metadata['version'], 'APP_COMMIT': metadata['source_sha']}
    subprocess.run(['uv', 'run', '--no-sync', 'python', '-m', *roles[sys.argv[1]]], cwd=root/'backend', env=env, check=True)
else:
    raise SystemExit('Usage: python install.py install|api|crawler|market|calendar|news-risk|jin10-calendar')
