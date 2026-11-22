#!/usr/bin/env python3
"""Promote the frozen remote dev tip to main from a trusted manual Actions run."""

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import urllib.parse
import urllib.request

FORGEJO_SERVER = 'https://git.134333.xyz'
FORGEJO_GIT = FORGEJO_SERVER + '/eezd/AlphaPulse.git'
AUTH_PREFIX = 'alphapulse-promote'
MAIN = 'refs/heads/main'
DEV = 'refs/heads/dev'
SHA = re.compile(r'[0-9a-f]{40}')


class SafeFailure(RuntimeError):
    """A diagnostic that contains no raw transport or credential-helper output."""


class PromotionInterrupted(KeyboardInterrupt):
    """A sanitized cancellation diagnostic that preserves the signal exit status."""

    def __init__(self, message, exit_code=130):
        super().__init__(message)
        self.exit_code = exit_code


def failure_with_cause(message, cause):
    if isinstance(cause, KeyboardInterrupt):
        return PromotionInterrupted(message, getattr(cause, 'exit_code', 130))
    return SafeFailure(message)


def require(condition, message):
    if not condition:
        raise SafeFailure(message)


def safe_reason(error):
    if isinstance(error, (SafeFailure, PromotionInterrupted)):
        return str(error)
    if isinstance(error, OSError):
        return f'传输或进程异常（{type(error).__name__}，errno={error.errno}）'
    if isinstance(error, ValueError):
        return '响应解析异常（ValueError）'
    return f'控制器异常（{type(error).__name__}）'


def ci_auth_directory():
    require(os.environ.get('CONTROL_EVENT') == 'workflow_dispatch', '只允许 workflow_dispatch 手动运行')
    require(os.environ.get('CONTROL_REF') == MAIN, '只允许从可信 main 手动运行')
    require(SHA.fullmatch(os.environ.get('CONTROL_SHA', '')), '缺少固定的可信控制 SHA')
    run_id, attempt = os.environ.get('CI_RUN_ID', ''), os.environ.get('CI_RUN_ATTEMPT', '')
    require(all(re.fullmatch(r'[1-9][0-9]*', value) for value in (run_id, attempt)), '无效的 CI run/attempt')
    parent = Path(os.environ.get('RUNNER_TEMP', '')).resolve()
    path = Path(os.environ.get('CI_AUTH_DIR', ''))
    expected = parent / f'{AUTH_PREFIX}-{run_id}-{attempt}'
    require(parent.is_dir() and path.is_absolute() and not path.is_symlink() and
            path.parent.resolve() == parent and path.resolve() == expected,
            '认证目录不属于当前 run/attempt')
    return path


def cleanup_auth():
    path = ci_auth_directory()
    if path.exists():
        shutil.rmtree(path)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def authenticated_login(token):
    request = urllib.request.Request(FORGEJO_SERVER + '/api/v1/user', headers={
        'Authorization': 'token ' + token, 'User-Agent': 'Forgejo manual branch promotion',
    })
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(request, timeout=30) as response:
            data = json.load(response)
    except (OSError, ValueError) as error:
        raise SafeFailure('Forgejo API 身份验证失败；请核对 RELEASE_TOKEN') from error
    login = data.get('login') if isinstance(data, dict) else None
    require(login == 'eezd', 'Forgejo API 身份不是预期的 eezd')
    return login


class Runtime:
    def __init__(self, directory):
        self.directory = directory
        self.config = directory / 'gitconfig'
        self.target = '尚未冻结'

    @staticmethod
    def write_private(path, content):
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            output.write(content)

    def configure_git(self, login, token):
        parsed = urllib.parse.urlsplit(FORGEJO_GIT)
        credential = self.directory / 'forgejo.credentials'
        encode = lambda value: urllib.parse.quote(value, safe='')
        self.write_private(credential, f'https://{encode(login)}:{encode(token)}@{parsed.netloc}{parsed.path}\n')
        helper = 'store --file=' + shlex.quote(str(credential))
        self.write_private(self.config, '\n'.join([
            '[credential]', '\thelper =', '\tuseHttpPath = true',
            f'[credential "{FORGEJO_GIT}"]', '\thelper = ' + json.dumps(helper),
            '[core]', '\thooksPath = /dev/null', '[init]', '\ttemplateDir =',
            '[http]', '\tfollowRedirects = false', '',
        ]))

    def run(self, args, phase, *, cwd=None, allowed=(0,)):
        # Pass no host Git configuration, askpass, API Secrets or registry credentials.
        environment = {
            'PATH': os.environ.get('PATH', os.defpath), 'HOME': str(self.directory),
            'XDG_CONFIG_HOME': str(self.directory), 'LC_ALL': 'C', 'GIT_TERMINAL_PROMPT': '0',
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_SYSTEM': '/dev/null',
            'GIT_CONFIG_GLOBAL': str(self.config),
        }
        result = subprocess.run(args, cwd=cwd or self.directory, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=environment)
        # Git stderr can contain credential-helper diagnostics; never echo it with Secrets.
        require(result.returncode in allowed, f'{phase}失败（退出码 {result.returncode}）；请核对权限、分支保护及远端状态')
        return result


class Snapshot:
    def __init__(self, runtime):
        self.runtime = runtime
        self.directory = runtime.directory / 'snapshot.git'
        runtime.run(['git', 'init', '--bare', str(self.directory)], '创建临时 Git 仓库')
        self.git('remote', 'add', 'source', FORGEJO_GIT)
        self.git('fetch', '--no-tags', '--no-recurse-submodules', 'source', f'{MAIN}:{MAIN}', f'{DEV}:{DEV}',
                 phase='冻结远程 main/dev')
        self.heads = self.parse(self.git('for-each-ref', '--format=%(objectname)%09%(refname)', MAIN, DEV).stdout)

    def git(self, *args, phase='读取 Git 历史', allowed=(0,)):
        return self.runtime.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'push.followTags=false',
                                 '-c', 'push.pushOption=', *args], phase, cwd=self.directory, allowed=allowed)

    @staticmethod
    def parse(raw):
        refs = {}
        try:
            for line in raw.decode().splitlines():
                sha, ref = line.split('\t', 1)
                require(SHA.fullmatch(sha) and ref in (MAIN, DEV) and ref not in refs, 'main/dev 引用格式异常')
                refs[ref] = sha
        except ValueError as error:
            raise SafeFailure('main/dev 引用响应解析失败') from error
        require(refs.keys() == {MAIN, DEV}, '远程缺少 main 或 dev 分支')
        return refs

    def remote(self):
        return self.parse(self.git('ls-remote', '--refs', 'source', MAIN, DEV, phase='复查远程 main/dev').stdout)

    def ancestor(self, older, newer):
        return self.git('merge-base', '--is-ancestor', older, newer, allowed=(0, 1)).returncode == 0


def promote(runtime, control_sha):
    phase = '冻结远程引用'
    attempted_push = False
    target = '尚未冻结'
    try:
        snapshot = Snapshot(runtime)
        frozen = snapshot.heads
        target = runtime.target = frozen[DEV]
        phase = '核验历史'
        require(snapshot.ancestor(control_sha, frozen[MAIN]), '可信控制 SHA 不在当前 main 历史中')
        print(f'冻结 main: {frozen[MAIN]}\n冻结 dev:  {target}', flush=True)
        if frozen[MAIN] == target:
            phase = '一致快照读回'
            require(snapshot.remote() == frozen, '一致快照读回时 main/dev 已变化')
            print('main/dev 已一致且读回确认，未尝试 push，远端零写入')
            return False
        require(snapshot.ancestor(frozen[MAIN], target), '拒绝更新：main/dev 分叉或更新会使 main 回退')
        commits = snapshot.git('log', '--reverse', '--format=%h %s', f'{frozen[MAIN]}..{target}').stdout
        print('即将纳入的提交：\n' + commits.decode(), flush=True)
        phase = '写前复查'
        require(snapshot.remote() == frozen, '写入前 main/dev 已变化；请核对后重新运行')
        # Ordinary push protects target-only commits; it is not an atomic lock against manual pushes.
        phase = '推送'
        attempted_push = True
        snapshot.git('push', '--porcelain', 'source', f'{target}:{MAIN}', phase='快进远程 main')
        phase = '写后读回'
        observed = snapshot.remote()
        require(observed == {MAIN: target, DEV: target}, '写后 main/dev 与冻结目标不一致')
        print(f'快进完成，远程 main: {target}', flush=True)
        return True
    except (Exception, KeyboardInterrupt) as error:
        reason = safe_reason(error)
        if attempted_push:
            raise failure_with_cause(f'{phase}结果未确认；main 可能已更新，冻结目标为 {target}；'
                                     f'请人工核对远端 main/dev，不自动回滚或重试；原因：{reason}', error) from error
        raise failure_with_cause(f'写前失败（{phase}）；未尝试 push，远端零写入；冻结目标为 {target}；'
                                 f'原因：{reason}', error) from error


def main():
    try:
        path = ci_auth_directory()
        token = os.environ.get('FORGEJO_TOKEN', '').strip()
        require(token, '缺少 RELEASE_TOKEN；不读取宿主 Token 或 credential helper')
        parsed = urllib.parse.urlsplit(FORGEJO_GIT)
        require(parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password and
                not parsed.query and not parsed.fragment and FORGEJO_GIT.startswith(FORGEJO_SERVER + '/'),
                '必须使用不含凭据的固定 Forgejo HTTPS Git URL')
        require(not path.exists(), '当前 run/attempt 认证目录已存在，请先核对清理')
        path.mkdir(mode=0o700)
    except (Exception, KeyboardInterrupt) as error:
        raise failure_with_cause('写前失败（认证准备）；未尝试 push，远端零写入；原因：' + safe_reason(error), error) from error
    primary_failure = None
    updated = False
    runtime = Runtime(path)
    try:
        try:
            login = authenticated_login(token)
            runtime.configure_git(login, token)
        except (Exception, KeyboardInterrupt) as error:
            raise failure_with_cause('写前失败（身份与 Git 认证）；未尝试 push，远端零写入；原因：' + safe_reason(error), error) from error
        updated = promote(runtime, os.environ['CONTROL_SHA'])
    except BaseException as error:
        # Retain the original write-state diagnostic and its cause even if cleanup also fails.
        primary_failure = error
        raise
    finally:
        try:
            cleanup_auth()
        except (Exception, KeyboardInterrupt) as error:
            cleanup_message = '认证清理失败；请人工核对当前 run/attempt 认证目录；原因：' + safe_reason(error)
            if primary_failure is not None:
                print(cleanup_message, file=sys.stderr, flush=True)
            else:
                outcome = 'main 已更新且读回确认' if updated else 'main/dev 已一致且读回确认，未尝试 push，远端零写入'
                raise failure_with_cause(f'{cleanup_message}；{outcome}；冻结目标为 {runtime.target}', error) from error


def terminate(signum, frame):
    raise PromotionInterrupted('收到 SIGTERM，操作被中断', 128 + signum)


def cli():
    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print('分支提升失败：' + safe_reason(error), file=sys.stderr, flush=True)
        return getattr(error, 'exit_code', 130) if isinstance(error, KeyboardInterrupt) else 1
    finally:
        signal.signal(signal.SIGTERM, previous)
    return 0


if __name__ == '__main__':
    sys.exit(cli())
