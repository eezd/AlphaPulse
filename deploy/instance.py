"""Start or stop the installed AlphaPulse instance without building or pulling."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess

SERVICES = ('api', 'web', 'market', 'crawler', 'calendar', 'trader', 'news-risk', 'jin10-calendar')
SEARCH_IMAGE = 'docker.io/searxng/searxng:2026.10.30-ffa88e3ba'
SEARCH_DIGEST = 'sha256:f4aa660c54d062dedb268be84e821ea24e39f48c7a496af8dd02cc9eedb93847'


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(args, env):
    result = subprocess.run(args, env=env, text=True, capture_output=True)
    # Do not include command output: Compose diagnostics may contain private configuration.
    require(result.returncode == 0, f'Docker 命令失败（退出码 {result.returncode}）：请检查实例配置与容器状态')
    return result.stdout.strip()


def start(directory, command, env):
    build = json.loads((directory / 'deployment.json').read_text())
    require(build.get('verified') is True, '部署回执未通过验收')
    require(re.fullmatch('[0-9a-f]{40}', build['commit']), '部署回执提交无效')
    require(re.fullmatch(r'\d{8}-' + build['commit'][:8], build['version']), '部署回执版本不匹配')
    images = {part: 'git.134333.xyz/eezd/dev/' + part + '/alphapulse:' + build['version'] for part in ('backend', 'web')}
    for part, image in images.items():
        require(re.fullmatch(r'git\.134333\.xyz/eezd/dev/' + part + r'/alphapulse:\d{8}-[0-9a-f]{8}', image), '部署镜像版本标签无效')
    # Inspect interpolation without reading service credentials into the controller.
    config = json.loads(run([*command, 'config', '--format', 'json', '--no-env-resolution'], env))
    services = set(config['services'])
    require(config['name'] == 'alphapulse-dev' and services in (set(SERVICES), set(SERVICES) | {'searxng'}), 'Compose 实例或服务集合不匹配')
    expected = {name: SEARCH_IMAGE if name == 'searxng' else images['web' if name == 'web' else 'backend'] for name in services}
    require(all(config['services'][name]['image'] == image for name, image in expected.items()), 'images.env 或 Compose 镜像与部署回执不一致')
    local_ids = {}
    for image in set(expected.values()):
        info, = json.loads(run(['docker', 'image', 'inspect', image], env))
        require(info['Architecture'] == 'amd64' and info['Os'] == 'linux', '本地镜像平台不匹配')
        if image == SEARCH_IMAGE:
            require(any(ref.rsplit('@', 1)[-1] == SEARCH_DIGEST for ref in info.get('RepoDigests', [])),
                    '本地搜索版本标签指向的镜像与验收摘要不一致')
        else:
            part = 'web' if image == images['web'] else 'backend'
            require(build['images'][part]['digest'] in info.get('RepoDigests', []), '本地版本标签指向的镜像与验收摘要不一致')
            labels = info['Config'].get('Labels') or {}
            require(labels.get('org.opencontainers.image.revision') == build['commit'] and
                    labels.get('org.opencontainers.image.version') == build['version'], '本地镜像身份与部署回执不一致')
        local_ids[image] = info['Id']
    run([*command, 'config', '--quiet'], env)
    run([*command, 'up', '-d', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '120'], env)
    identifiers = run([*command, 'ps', '--all', '--quiet'], env).split()
    require(len(identifiers) == len(services), '启动后服务数量不完整')
    template = '{"service":{{json (index .Config.Labels "com.docker.compose.service")}},"image":{{json .Image}},"state":{{json .State}}}'
    containers = [json.loads(line) for line in run(['docker', 'inspect', '--format', template, *identifiers], env).splitlines()]
    require({c['service'] for c in containers} == services, '启动后服务集合不完整')
    for container in containers:
        state = container['state']
        require(state['Running'] and not state['Paused'] and state.get('Health', {}).get('Status', 'healthy') == 'healthy',
                f"服务未健康运行：{container['service']}")
        require(container['image'] == local_ids[expected[container['service']]], '运行镜像与部署回执不一致')


def control(directory, action):
    with (directory / '.deploy.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('实例正在部署或启停，请稍后重试') from None
        require(action != 'start' or not (directory / 'deployment.pending.json').exists(), '实例存在未完成部署，请先恢复部署状态')
        files = [directory / name for name in ('compose.yaml', 'compose.yml', 'docker-compose.yaml', 'docker-compose.yml') if (directory / name).is_file()]
        require(len(files) == 1, '实例必须有且只有一个 Compose 主文件')
        require((directory / 'images.env').is_file(), '缺少实例 images.env，请由部署流程补齐')
        env = {k: v for k, v in os.environ.items() if not k.startswith(('COMPOSE_', 'ALPHAPULSE_'))}
        command = ['docker', 'compose', '--project-directory', str(directory), '--project-name', 'alphapulse-dev',
                   '--env-file', str(directory / 'images.env'), '-f', str(files[0])]
        if action == 'start':
            start(directory, command, env)
        else:
            run([*command, 'config', '--quiet'], env)
            run([*command, 'stop'], env)
        print('实例已健康启动' if action == 'start' else '实例已停止', '：', directory.name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'stop'))
    args = parser.parse_args()
    try:
        control(Path(__file__).resolve().parent, args.action)
    except (RuntimeError, OSError, ValueError, KeyError, TypeError) as error:
        # Only report controller messages; malformed private files must not leak their contents.
        parser.exit(1, (str(error) if isinstance(error, RuntimeError) else '实例文件或 Docker 数据无效，请检查部署状态') + '\n')


if __name__ == '__main__':
    main()
