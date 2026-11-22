import contextlib
import io
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/promote-main.py'
WORKFLOW = ROOT / '.forgejo/workflows/promote-main.yml'

promote = types.ModuleType('promote_main_test')
exec(compile(SCRIPT.read_text(), str(SCRIPT), 'exec'), promote.__dict__)


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / 'remote.git'
        self.seed = self.root / 'seed'
        self.seed.mkdir()
        self.environment = {'PATH': os.environ.get('PATH', os.defpath), 'HOME': str(self.root),
                            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null', 'LC_ALL': 'C'}
        self.git('init', '--bare', str(self.remote))
        self.git('init', '-b', 'main', str(self.seed))
        self.base = self.commit('base')
        self.git('checkout', '-b', 'dev')
        self.tip = self.commit('feature')
        self.git('remote', 'add', 'origin', str(self.remote))
        self.git('push', 'origin', 'main', 'dev')
        self.git('tag', 'keep-tag', self.base)
        self.git('branch', 'keep-branch', self.base)
        self.git('push', 'origin', 'keep-tag', 'keep-branch')
        directory = self.root / 'auth'
        directory.mkdir(mode=0o700)
        self.runtime = promote.Runtime(directory)
        self.runtime.configure_git('synthetic-user', 'synthetic-token')
        self.patches = patch.object(promote, 'FORGEJO_GIT', str(self.remote))
        self.patches.start()
        self.addCleanup(self.patches.stop)
        self.calls = []
        real_run = self.runtime.run

        def run(args, phase, **kwargs):
            self.calls.append(args)
            return real_run(args, phase, **kwargs)

        self.runtime.run = run

    def git(self, *args):
        return subprocess.check_output(['git', '-c', 'user.name=Synthetic Test',
                                        '-c', 'user.email=test@example.invalid', *args], cwd=self.seed,
                                       env=self.environment, stderr=subprocess.PIPE).decode().strip()

    def commit(self, name):
        (self.seed / (name + '.txt')).write_text(name)
        self.git('add', '.')
        self.git('commit', '-m', name)
        return self.git('rev-parse', 'HEAD')

    def refs(self):
        return dict(line.split('\t')[::-1] for line in self.git('ls-remote', '--refs', 'origin').splitlines())

    def pushes(self):
        return [args for args in self.calls if 'push' in args]

    def execute(self, control=None):
        with contextlib.redirect_stdout(io.StringIO()):
            return promote.promote(self.runtime, control or self.base)

    def advance(self, branch, name):
        self.git('checkout', branch)
        sha = self.commit(name)
        self.git('push', 'origin', branch)
        return sha

    def test_fast_forward_changes_only_main_with_frozen_sha_and_no_force(self):
        before = self.refs()
        self.assertTrue(self.execute())
        after = self.refs()
        self.assertEqual(after, {**before, promote.MAIN: self.tip})
        self.assertEqual(len(self.pushes()), 1)
        self.assertEqual(self.pushes()[0][-1], self.tip + ':' + promote.MAIN)
        self.assertFalse(any(arg.startswith(('--force', '+')) for arg in self.pushes()[0]))
        fetch = next(args for args in self.calls if 'fetch' in args)
        self.assertIn('--no-tags', fetch)
        self.assertNotIn('checkout', [arg for args in self.calls for arg in args])
        self.assertEqual(self.git('rev-parse', 'main'), self.base)

    def test_equal_tips_are_a_zero_write_success(self):
        self.git('push', 'origin', self.tip + ':refs/heads/main')
        before = self.refs()
        self.assertFalse(self.execute())
        self.assertEqual(self.refs(), before)
        self.assertEqual(self.pushes(), [])
        self.assertEqual(sum('ls-remote' in args for args in self.calls), 1)

    def test_equal_tips_readback_failure_remains_zero_write(self):
        self.git('push', 'origin', self.tip + ':refs/heads/main')
        before = self.refs()
        with patch.object(promote.Snapshot, 'remote', side_effect=OSError('synthetic-secret')):
            with self.assertRaisesRegex(RuntimeError, '未尝试 push，远端零写入') as failure:
                self.execute()
        self.assertNotIn('main 可能已更新', str(failure.exception))
        self.assertNotIn('synthetic-secret', str(failure.exception))
        self.assertEqual(self.refs(), before)
        self.assertEqual(self.pushes(), [])

    def test_divergence_is_rejected(self):
        self.advance('main', 'main-only')
        before = self.refs()
        with self.assertRaisesRegex(RuntimeError, '分叉或更新会使 main 回退'):
            self.execute()
        self.assertEqual(self.refs(), before)
        self.assertEqual(self.pushes(), [])

    def test_backward_update_is_rejected(self):
        self.git('--git-dir=' + str(self.remote), 'update-ref', promote.MAIN, self.tip)
        self.git('--git-dir=' + str(self.remote), 'update-ref', promote.DEV, self.base)
        before = self.refs()
        with self.assertRaisesRegex(RuntimeError, '分叉或更新会使 main 回退'):
            self.execute()
        self.assertEqual(self.refs(), before)
        self.assertEqual(self.pushes(), [])

    def test_control_sha_must_belong_to_frozen_main(self):
        with self.assertRaisesRegex(RuntimeError, '可信控制 SHA'):
            self.execute(self.tip)
        self.assertEqual(self.pushes(), [])

    def test_missing_branch_fails_before_any_push(self):
        self.git('push', 'origin', '--delete', 'dev')
        before = self.refs()
        with self.assertRaises(RuntimeError):
            self.execute()
        self.assertEqual(self.refs(), before)
        self.assertEqual(self.pushes(), [])

    def test_source_movement_before_push_does_not_silently_change_target(self):
        original = promote.Snapshot.remote

        def changed(snapshot):
            self.advance('dev', 'newer-feature')
            return original(snapshot)

        with patch.object(promote.Snapshot, 'remote', changed):
            with self.assertRaisesRegex(RuntimeError, '写入前 main/dev 已变化'):
                self.execute()
        self.assertEqual(self.refs()[promote.MAIN], self.base)
        self.assertEqual(self.pushes(), [])

    def test_target_movement_before_push_is_rejected(self):
        original = promote.Snapshot.remote

        def changed(snapshot):
            self.external = self.advance('main', 'new-main')
            return original(snapshot)

        with patch.object(promote.Snapshot, 'remote', changed):
            with self.assertRaisesRegex(RuntimeError, '写入前 main/dev 已变化'):
                self.execute()
        self.assertEqual(self.refs()[promote.MAIN], self.external)
        self.assertEqual(self.pushes(), [])

    def test_server_permission_rejection_does_not_bypass_protection(self):
        hook = self.remote / 'hooks/pre-receive'
        hook.write_text('#!/bin/sh\necho "synthetic-secret protected branch" >&2\nexit 1\n')
        hook.chmod(0o700)
        # Local Git transport shares client -c options; HTTPS receivers do not.
        receiver = self.root / 'receive-pack'
        receiver.write_text('#!/bin/sh\nunset GIT_CONFIG_PARAMETERS GIT_CONFIG_GLOBAL GIT_CONFIG_COUNT\n'
                            + 'exec git -c core.hooksPath=' + shlex.quote(str(hook.parent))
                            + ' receive-pack "$@"\n')
        receiver.chmod(0o700)
        original = self.runtime.run

        def with_receiver(args, phase, **kwargs):
            if 'push' in args:
                args = [*args[:args.index('push') + 1], '--receive-pack=' + str(receiver),
                        *args[args.index('push') + 1:]]
            return original(args, phase, **kwargs)

        self.runtime.run = with_receiver
        before = self.refs()
        with self.assertRaisesRegex(RuntimeError, '快进远程 main失败') as failure:
            self.execute()
        self.assertNotIn('synthetic-secret', str(failure.exception))
        self.assertEqual(self.refs(), before)
        self.assertEqual(len(self.pushes()), 1)

    def test_external_main_write_at_push_is_not_overwritten(self):
        original = self.runtime.run

        def changed(args, phase, **kwargs):
            if 'push' in args:
                self.external = self.advance('main', 'external-main')
            return original(args, phase, **kwargs)

        self.runtime.run = changed
        with self.assertRaisesRegex(RuntimeError, '快进远程 main失败'):
            self.execute()
        self.assertEqual(self.refs()[promote.MAIN], self.external)
        self.assertEqual(self.refs()[promote.DEV], self.tip)

    def test_pre_push_transport_failure_does_not_claim_an_uncertain_write(self):
        before = self.refs()
        original = promote.subprocess.run

        def disconnected(args, **kwargs):
            if 'ls-remote' in args:
                return subprocess.CompletedProcess(args, 128, b'', b'synthetic-secret connection lost')
            return original(args, **kwargs)

        with patch.object(promote.subprocess, 'run', side_effect=disconnected):
            with self.assertRaisesRegex(RuntimeError, '复查远程 main/dev失败（退出码 128）') as failure:
                self.execute()
        self.assertNotIn('main 可能已更新', str(failure.exception))
        self.assertIn('未尝试 push，远端零写入', str(failure.exception))
        self.assertIsNotNone(failure.exception.__cause__)
        self.assertNotIn('synthetic-secret', str(failure.exception))
        self.assertEqual(self.refs(), before)
        self.assertEqual(self.pushes(), [])

    def assert_uncertain_readback(self, response, reason):
        before = self.refs()
        original = promote.subprocess.run
        pushed = False

        def disconnected(args, **kwargs):
            nonlocal pushed
            if pushed and 'ls-remote' in args:
                if isinstance(response, Exception):
                    raise response
                return response
            result = original(args, **kwargs)
            if 'push' in args:
                pushed = True
            return result

        output = io.StringIO()
        with patch.object(promote.subprocess, 'run', side_effect=disconnected), \
                contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError, '写后读回结果未确认.*main 可能已更新') as failure:
                promote.promote(self.runtime, self.base)
        self.assertIn(reason, str(failure.exception))
        self.assertIn(self.tip, str(failure.exception))
        self.assertIn('不自动回滚或重试', str(failure.exception))
        self.assertIsNotNone(failure.exception.__cause__)
        self.assertNotIn('synthetic-secret', str(failure.exception))
        self.assertNotIn('快进完成', output.getvalue())
        self.assertEqual(self.refs(), {**before, promote.MAIN: self.tip})
        self.assertEqual(len(self.pushes()), 1)

    def test_readback_transport_exit_after_real_push_reports_uncertainty(self):
        response = subprocess.CompletedProcess([], 128, b'', b'synthetic-secret connection lost')
        self.assert_uncertain_readback(response, '复查远程 main/dev失败（退出码 128）')

    def test_readback_os_error_after_real_push_reports_uncertainty(self):
        self.assert_uncertain_readback(OSError('synthetic-secret readback timeout'), '传输或进程异常')

    def test_malformed_readback_after_real_push_reports_uncertainty(self):
        response = subprocess.CompletedProcess([], 0, b'invalid-ref', b'')
        self.assert_uncertain_readback(response, 'main/dev 引用响应解析失败')

    def test_unexpected_readback_exception_after_real_push_is_sanitized(self):
        self.assert_uncertain_readback(TypeError('synthetic-secret malformed response'), '控制器异常（TypeError）')

    def test_readback_invalid_encoding_after_real_push_is_sanitized(self):
        response = subprocess.CompletedProcess([], 0, b'\xffsynthetic-secret', b'')
        self.assert_uncertain_readback(response, 'main/dev 引用响应解析失败')

    def test_readback_missing_ref_after_real_push_reports_uncertainty(self):
        response = subprocess.CompletedProcess([], 0, (self.tip + '\t' + promote.MAIN).encode(), b'')
        self.assert_uncertain_readback(response, '远程缺少 main 或 dev 分支')

    def test_lost_push_acknowledgment_reports_uncertainty_without_retry(self):
        before = self.refs()
        original = promote.subprocess.run

        def lost_acknowledgment(args, **kwargs):
            result = original(args, **kwargs)
            if 'push' in args:
                self.assertEqual(result.returncode, 0)
                return subprocess.CompletedProcess(args, 128, b'', b'synthetic-secret acknowledgment lost')
            return result

        output = io.StringIO()
        with patch.object(promote.subprocess, 'run', side_effect=lost_acknowledgment), \
                contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError, '推送结果未确认.*main 可能已更新') as failure:
                promote.promote(self.runtime, self.base)
        self.assertIn('快进远程 main失败（退出码 128）', str(failure.exception))
        self.assertIn(self.tip, str(failure.exception))
        self.assertIn('不自动回滚或重试', str(failure.exception))
        self.assertIsNotNone(failure.exception.__cause__)
        self.assertNotIn('synthetic-secret', str(failure.exception))
        self.assertNotIn('快进完成', output.getvalue())
        self.assertEqual(self.refs(), {**before, promote.MAIN: self.tip})
        self.assertEqual(len(self.pushes()), 1)
        self.assertEqual(sum('ls-remote' in args for args in self.calls), 1)

    def test_readback_failure_reports_partial_write_without_retry_or_rollback(self):
        original = self.runtime.run

        def changed(args, phase, **kwargs):
            result = original(args, phase, **kwargs)
            if 'push' in args:
                self.advance('dev', 'post-push-feature')
            return result

        self.runtime.run = changed
        with self.assertRaisesRegex(RuntimeError, '写后读回结果未确认.*main 可能已更新') as failure:
            self.execute()
        self.assertIn(self.tip, str(failure.exception))
        self.assertIn('写后 main/dev 与冻结目标不一致', str(failure.exception))
        self.assertIsNotNone(failure.exception.__cause__)
        self.assertEqual(self.refs()[promote.MAIN], self.tip)
        self.assertEqual(len(self.pushes()), 1)

    def test_main_entry_cleans_auth_after_real_push_and_transport_exception(self):
        directory = self.root / (promote.AUTH_PREFIX + '-12-2')
        neighbor = self.root / (promote.AUTH_PREFIX + '-13-1')
        neighbor.mkdir()
        (neighbor / 'keep').write_text('other run')
        before = self.refs()
        original = promote.Runtime.run
        pushes = []
        def lost_ack(runtime, args, phase, **kwargs):
            args = [str(self.remote) if arg == https_url else arg for arg in args]
            result = original(runtime, args, phase, **kwargs)
            if 'push' in args:
                pushes.append(args)
                self.assertEqual(result.returncode, 0)
                raise OSError('synthetic-secret acknowledgment lost')
            return result
        https_url = 'https://git.134333.xyz/eezd/AlphaPulse.git'
        environment = {'PATH': self.environment['PATH'], 'HOME': str(self.root / 'unused-home'),
                       'CONTROL_EVENT': 'workflow_dispatch', 'CONTROL_REF': promote.MAIN,
                       'CONTROL_SHA': self.base, 'CI_RUN_ID': '12', 'CI_RUN_ATTEMPT': '2',
                       'RUNNER_TEMP': str(self.root), 'CI_AUTH_DIR': str(directory),
                       'FORGEJO_TOKEN': 'synthetic-secret'}
        output = io.StringIO()
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(promote, 'FORGEJO_GIT', https_url), \
                patch.object(promote, 'authenticated_login', return_value='eezd'), \
                patch.object(promote.Runtime, 'run', lost_ack), contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError, '推送结果未确认.*main 可能已更新') as failure:
                promote.main()
        self.assertIn(self.tip, str(failure.exception))
        self.assertNotIn('synthetic-secret', str(failure.exception))
        self.assertIsInstance(failure.exception.__cause__, OSError)
        self.assertNotIn('快进完成', output.getvalue())
        self.assertEqual(self.refs(), {**before, promote.MAIN: self.tip})
        self.assertEqual(len(pushes), 1)
        self.assertFalse(directory.exists())
        self.assertEqual((neighbor / 'keep').read_text(), 'other run')


    @contextlib.contextmanager
    def main_entry(self, fault=None, cleanup_error=None):
        directory = self.root / (promote.AUTH_PREFIX + '-12-2')
        neighbor = self.root / (promote.AUTH_PREFIX + '-13-1')
        neighbor.mkdir(exist_ok=True)
        (neighbor / 'keep').write_text('other run')
        https_url = 'https://git.134333.xyz/eezd/AlphaPulse.git'
        environment = {'PATH': self.environment['PATH'], 'HOME': str(self.root / 'unused-home'),
                       'CONTROL_EVENT': 'workflow_dispatch', 'CONTROL_REF': promote.MAIN,
                       'CONTROL_SHA': self.base, 'CI_RUN_ID': '12', 'CI_RUN_ATTEMPT': '2',
                       'RUNNER_TEMP': str(self.root), 'CI_AUTH_DIR': str(directory),
                       'FORGEJO_TOKEN': 'synthetic-secret'}
        original = promote.Runtime.run

        def run(runtime, args, phase, **kwargs):
            args = [str(self.remote) if arg == https_url else arg for arg in args]
            self.calls.append(args)
            result = original(runtime, args, phase, **kwargs)
            if fault is not None:
                fault(args, result)
            return result

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, environment, clear=True))
            stack.enter_context(patch.object(promote, 'FORGEJO_GIT', https_url))
            stack.enter_context(patch.object(promote, 'authenticated_login', return_value='eezd'))
            stack.enter_context(patch.object(promote.Runtime, 'run', run))
            output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            errors = stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            if cleanup_error is not None:
                stack.enter_context(patch.object(promote, 'cleanup_auth', side_effect=cleanup_error))
            yield directory, output, errors
            self.assertEqual((neighbor / 'keep').read_text(), 'other run')

    def test_cleanup_failure_preserves_primary_uncertainty_and_cause(self):
        before = self.refs()
        lost_ack = OSError('synthetic-secret acknowledgment lost')
        denied = PermissionError(13, 'synthetic-secret cleanup denied')

        def fault(args, result):
            if 'push' in args:
                self.assertEqual(result.returncode, 0)
                raise lost_ack

        with self.main_entry(fault, denied) as (directory, output, errors):
            with self.assertRaisesRegex(promote.SafeFailure, '推送结果未确认.*main 可能已更新') as failure:
                promote.main()
            self.assertIs(failure.exception.__cause__, lost_ack)
            self.assertIn(self.tip, promote.safe_reason(failure.exception))
            self.assertIn('不自动回滚或重试', str(failure.exception))
            self.assertIn('认证清理失败', errors.getvalue())
            self.assertIn('PermissionError', errors.getvalue())
            self.assertTrue(directory.exists())
            self.assertNotIn('synthetic-secret', str(failure.exception) + errors.getvalue())
            self.assertNotIn('快进完成', output.getvalue())
        self.assertEqual(self.refs(), {**before, promote.MAIN: self.tip})
        self.assertEqual(len(self.pushes()), 1)

    def test_cli_reports_primary_and_cleanup_failures_without_traceback(self):
        before = self.refs()

        def fault(args, result):
            if 'push' in args:
                self.assertEqual(result.returncode, 0)
                raise OSError('synthetic-secret acknowledgment lost')

        with self.main_entry(fault, PermissionError(13, 'synthetic-secret')) as (_, output, errors):
            self.assertEqual(promote.cli(), 1)
            self.assertIn('推送结果未确认', errors.getvalue())
            self.assertIn('main 可能已更新', errors.getvalue())
            self.assertIn(self.tip, errors.getvalue())
            self.assertIn('认证清理失败', errors.getvalue())
            self.assertNotIn('synthetic-secret', output.getvalue() + errors.getvalue())
            self.assertNotIn('Traceback', errors.getvalue())
        self.assertEqual(self.refs(), {**before, promote.MAIN: self.tip})
        self.assertEqual(len(self.pushes()), 1)

    def test_cleanup_failure_after_confirmed_push_reports_the_known_update(self):
        before = self.refs()
        denied = PermissionError(13, 'synthetic-secret cleanup denied')
        with self.main_entry(cleanup_error=denied) as (directory, _, errors):
            with self.assertRaisesRegex(promote.SafeFailure, '认证清理失败.*main 已更新且读回确认') as failure:
                promote.main()
            self.assertIs(failure.exception.__cause__, denied)
            self.assertIn(self.tip, str(failure.exception))
            self.assertNotIn('远端零写入', str(failure.exception))
            self.assertNotIn('synthetic-secret', str(failure.exception) + errors.getvalue())
            self.assertTrue(directory.exists())
        self.assertEqual(self.refs(), {**before, promote.MAIN: self.tip})
        self.assertEqual(len(self.pushes()), 1)

    def test_cleanup_failure_after_equal_readback_still_reports_zero_write(self):
        self.git('push', 'origin', self.tip + ':' + promote.MAIN)
        before = self.refs()
        with self.main_entry(cleanup_error=PermissionError(13, 'synthetic-secret')):
            with self.assertRaisesRegex(promote.SafeFailure, '认证清理失败.*未尝试 push，远端零写入') as failure:
                promote.main()
            self.assertIn(self.tip, str(failure.exception))
            self.assertNotIn('main 可能已更新', str(failure.exception))
        self.assertEqual(self.refs(), before)
        self.assertEqual(self.pushes(), [])

    def test_interrupt_after_real_push_or_during_readback_preserves_uncertainty(self):
        for point in ('push', 'readback'):
            with self.subTest(point=point):
                self.git('--git-dir=' + str(self.remote), 'update-ref', promote.MAIN, self.base)
                self.calls.clear()
                before = self.refs()
                interrupted = KeyboardInterrupt('synthetic-secret')
                pushed = False

                def fault(args, result, point=point, interrupted=interrupted):
                    nonlocal pushed
                    if 'push' in args:
                        self.assertEqual(result.returncode, 0)
                        pushed = True
                        if point == 'push':
                            raise interrupted
                    elif pushed and 'ls-remote' in args:
                        raise interrupted

                with self.main_entry(fault) as (directory, output, errors):
                    with self.assertRaisesRegex(promote.PromotionInterrupted, 'main 可能已更新') as failure:
                        promote.main()
                    self.assertIs(failure.exception.__cause__, interrupted)
                    self.assertIn('推送' if point == 'push' else '写后读回', str(failure.exception))
                    self.assertIn(self.tip, str(failure.exception))
                    self.assertEqual(failure.exception.exit_code, 130)
                    self.assertNotIn('synthetic-secret', str(failure.exception) + errors.getvalue())
                    self.assertNotIn('快进完成', output.getvalue())
                    self.assertFalse(directory.exists())
                    promote.cleanup_auth()
                self.assertEqual(self.refs(), {**before, promote.MAIN: self.tip})
                self.assertEqual(len(self.pushes()), 1)

    def test_cli_keeps_interrupt_exit_status_when_cleanup_also_fails(self):
        before = self.refs()

        def fault(args, result):
            if 'push' in args:
                self.assertEqual(result.returncode, 0)
                raise KeyboardInterrupt('synthetic-secret')

        with self.main_entry(fault, PermissionError(13, 'synthetic-secret')) as (_, output, errors):
            self.assertEqual(promote.cli(), 130)
            self.assertIn('main 可能已更新', errors.getvalue())
            self.assertIn(self.tip, errors.getvalue())
            self.assertIn('认证清理失败', errors.getvalue())
            self.assertNotIn('synthetic-secret', output.getvalue() + errors.getvalue())
            self.assertNotIn('快进完成', output.getvalue())
        self.assertEqual(self.refs(), {**before, promote.MAIN: self.tip})
        self.assertEqual(len(self.pushes()), 1)

    def test_cli_sigterm_after_real_push_reports_uncertainty_and_restores_handler(self):
        for point in ('push', 'readback'):
            with self.subTest(point=point):
                self.git('--git-dir=' + str(self.remote), 'update-ref', promote.MAIN, self.base)
                self.calls.clear()
                before = self.refs()
                previous = promote.signal.getsignal(promote.signal.SIGTERM)
                pushed = False

                def fault(args, result, point=point):
                    nonlocal pushed
                    if 'push' in args:
                        self.assertEqual(result.returncode, 0)
                        pushed = True
                    if (point == 'push' and 'push' in args) or (point == 'readback' and pushed and 'ls-remote' in args):
                        # Signal only this test process, after verifying our handler is installed.
                        self.assertIs(promote.signal.getsignal(promote.signal.SIGTERM), promote.terminate)
                        os.kill(os.getpid(), promote.signal.SIGTERM)

                with self.main_entry(fault) as (directory, output, errors):
                    self.assertEqual(promote.cli(), 143)
                    self.assertIn('推送' if point == 'push' else '写后读回', errors.getvalue())
                    self.assertIn('main 可能已更新', errors.getvalue())
                    self.assertIn(self.tip, errors.getvalue())
                    self.assertIn('SIGTERM', errors.getvalue())
                    self.assertNotIn('synthetic-secret', errors.getvalue())
                    self.assertNotIn('Traceback', errors.getvalue())
                    self.assertNotIn('快进完成', output.getvalue())
                    self.assertFalse(directory.exists())
                    promote.cleanup_auth()
                self.assertIs(promote.signal.getsignal(promote.signal.SIGTERM), previous)
                self.assertEqual(self.refs(), {**before, promote.MAIN: self.tip})
                self.assertEqual(len(self.pushes()), 1)


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / (promote.AUTH_PREFIX + '-12-2')
        self.environment = {'PATH': os.environ.get('PATH', os.defpath), 'HOME': str(self.root / 'host-home'),
                            'CONTROL_EVENT': 'workflow_dispatch', 'CONTROL_REF': promote.MAIN, 'CONTROL_SHA': 'a' * 40,
                            'CI_RUN_ID': '12', 'CI_RUN_ATTEMPT': '2', 'RUNNER_TEMP': str(self.root),
                            'CI_AUTH_DIR': str(self.directory), 'FORGEJO_TOKEN': 'synthetic-secret'}
        self.env = patch.dict(os.environ, self.environment, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_missing_secret_does_not_read_home_credentials_or_call_network(self):
        path = Path(os.environ['HOME']) / '.config/forgejo/token'
        path.parent.mkdir(parents=True)
        path.write_text('host-secret-must-not-be-read')
        os.environ['FORGEJO_TOKEN'] = ''
        with patch.object(promote, 'authenticated_login') as login, patch.object(promote, 'promote') as action:
            with self.assertRaisesRegex(RuntimeError, '缺少 RELEASE_TOKEN'):
                promote.main()
            login.assert_not_called()
            action.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_main_cleans_private_credentials_on_success_and_git_failure(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                def action(runtime, sha, failure=failure):
                    self.assertEqual(sha, 'a' * 40)
                    self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
                    self.assertEqual(runtime.config.stat().st_mode & 0o777, 0o600)
                    self.assertEqual((self.directory / 'forgejo.credentials').stat().st_mode & 0o777, 0o600)
                    if failure:
                        raise RuntimeError('synthetic Git permission failure')
                with patch.object(promote, 'authenticated_login', return_value='tester'), \
                        patch.object(promote, 'promote', side_effect=action):
                    if failure:
                        with self.assertRaisesRegex(RuntimeError, 'synthetic Git permission failure'):
                            promote.main()
                    else:
                        promote.main()
                self.assertFalse(self.directory.exists())

    def test_api_failure_also_cleans_directory(self):
        with patch.object(promote, 'authenticated_login', side_effect=promote.SafeFailure('permission denied')):
            with self.assertRaisesRegex(RuntimeError, '写前失败.*permission denied'):
                promote.main()
        self.assertFalse(self.directory.exists())

    def test_api_interrupt_is_chained_and_sanitized_as_a_zero_write_cancellation(self):
        interrupted = KeyboardInterrupt('synthetic-secret')
        with patch.object(promote, 'authenticated_login', side_effect=interrupted), \
                patch.object(promote, 'promote') as action:
            with self.assertRaisesRegex(promote.PromotionInterrupted, '未尝试 push，远端零写入') as failure:
                promote.main()
            self.assertIs(failure.exception.__cause__, interrupted)
            self.assertNotIn('synthetic-secret', str(failure.exception))
            self.assertNotIn('main 可能已更新', str(failure.exception))
            action.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_identity_and_path_guards_refuse_other_runs_or_symlinks(self):
        for key, value in [('CONTROL_EVENT', 'push'), ('CONTROL_REF', 'refs/heads/dev'), ('CONTROL_SHA', 'bad'), ('CI_RUN_ID', '0'),
                           ('CI_RUN_ATTEMPT', '../other'), ('CI_AUTH_DIR', str(self.root / 'other'))]:
            with self.subTest(key=key), patch.dict(os.environ, {key: value}):
                with self.assertRaises(RuntimeError):
                    promote.cleanup_auth()
        outside = self.root / 'outside'
        outside.mkdir()
        marker = outside / 'keep'
        marker.write_text('keep')
        self.directory.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            promote.cleanup_auth()
        self.assertTrue(marker.exists())

    def test_existing_auth_directory_is_not_reused(self):
        self.directory.mkdir()
        marker = self.directory / 'keep'
        marker.write_text('existing')
        with self.assertRaisesRegex(RuntimeError, '认证目录已存在'):
            promote.main()
        self.assertTrue(marker.exists())

    def test_cancellation_cleanup_and_always_cleanup_are_idempotent_and_scoped(self):
        neighbor = self.root / (promote.AUTH_PREFIX + '-12-3')
        neighbor.mkdir()
        marker = neighbor / 'keep'
        marker.write_text('other attempt')
        with patch.object(promote, 'authenticated_login', return_value='eezd'), \
                patch.object(promote, 'promote', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                promote.main()
        self.assertFalse(self.directory.exists())
        promote.cleanup_auth()
        promote.cleanup_auth()
        self.assertTrue(marker.exists())

    def test_api_rejection_and_parse_failures_are_chained_and_sanitized(self):
        for error in (OSError('synthetic-secret denied'), ValueError('synthetic-secret malformed')):
            with self.subTest(error=type(error).__name__):
                opener = Mock()
                opener.open.side_effect = error
                with patch.object(promote.urllib.request, 'build_opener', return_value=opener):
                    with self.assertRaisesRegex(RuntimeError, 'Forgejo API 身份验证失败') as failure:
                        promote.authenticated_login('synthetic-secret')
                self.assertIs(failure.exception.__cause__, error)
                self.assertNotIn('synthetic-secret', str(failure.exception))

    def test_unexpected_api_identity_fails_before_any_git_or_push(self):
        response = Mock()
        response.read.return_value = b'{"login":"unexpected"}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch.object(promote.urllib.request, 'build_opener', return_value=opener), \
                patch.object(promote.Runtime, 'run') as git:
            with self.assertRaisesRegex(RuntimeError, '未尝试 push，远端零写入.*不是预期的 eezd'):
                promote.main()
            git.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_git_receives_isolated_environment_and_redacted_errors(self):
        self.directory.mkdir()
        runtime = promote.Runtime(self.directory)
        runtime.configure_git('test-user', os.environ['FORGEJO_TOKEN'])
        os.environ.update({'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'credential.helper',
                           'GIT_CONFIG_VALUE_0': '!evil-helper', 'GIT_ASKPASS': '/host/askpass',
                           'REGISTRY_AUTH_FILE': '/host/registry.json', 'GITHUB_TOKEN': 'host-github'})
        result = subprocess.CompletedProcess([], 1, b'', b'synthetic-secret host-github')
        with patch.object(promote.subprocess, 'run', return_value=result) as run:
            with self.assertRaises(RuntimeError) as failure:
                runtime.run(['git', 'ls-remote', promote.FORGEJO_GIT], 'Git authentication')
        self.assertNotIn('synthetic-secret', str(failure.exception))
        self.assertNotIn('host-github', str(failure.exception))
        args, kwargs = run.call_args
        self.assertNotIn('synthetic-secret', repr(args))
        self.assertNotIn('synthetic-secret', repr(kwargs))
        self.assertEqual(kwargs['env']['HOME'], str(self.directory))
        self.assertEqual(kwargs['env']['GIT_CONFIG_GLOBAL'], str(runtime.config))
        self.assertFalse({'GIT_ASKPASS', 'GIT_CONFIG_COUNT', 'FORGEJO_TOKEN', 'GITHUB_TOKEN',
                          'REGISTRY_AUTH_FILE'} & kwargs['env'].keys())

    def test_git_credential_is_scoped_to_exact_repository_and_not_host_helpers(self):
        self.directory.mkdir()
        runtime = promote.Runtime(self.directory)
        runtime.configure_git('test-user', 'synthetic-secret')
        original = promote.subprocess.run
        # Exercise real Git credential matching, with stdin supplied only by this test.
        def with_input(args, **kwargs):
            return original(args, input=(self.query + '\n\n').encode(), **kwargs)
        with patch.object(promote.subprocess, 'run', side_effect=with_input):
            self.query = 'url=' + promote.FORGEJO_GIT
            result = runtime.run(['git', 'credential', 'fill'], 'credential test')
            self.assertIn(b'password=synthetic-secret', result.stdout)
            self.query = 'url=' + promote.FORGEJO_GIT + '-different'
            result = runtime.run(['git', 'credential', 'fill'], 'other repository', allowed=(128,))
            self.assertNotIn(b'synthetic-secret', result.stdout)

    def test_api_uses_secret_only_in_header_and_refuses_redirects(self):
        response = Mock()
        response.read.return_value = b'{"login":"eezd"}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch.object(promote.urllib.request, 'build_opener', return_value=opener) as build_opener:
            self.assertEqual(promote.authenticated_login('synthetic-secret'), 'eezd')
        self.assertEqual(build_opener.call_args.args[0].proxies, {})
        request = opener.open.call_args.args[0]
        self.assertNotIn('synthetic-secret', request.full_url)
        self.assertEqual(request.get_header('Authorization'), 'token synthetic-secret')
        self.assertIsNone(promote.NoRedirect().redirect_request(request, None, 302, '', {}, 'https://other.invalid'))


class WorkflowTests(unittest.TestCase):
    def test_manual_trusted_main_shared_concurrency_and_only_forgejo_secret(self):
        text = WORKFLOW.read_text()
        self.assertEqual(text.split('\non:\n', 1)[1].split('\nconcurrency:', 1)[0].strip(), 'workflow_dispatch:')
        self.assertIn("if: ${{ forgejo.ref == 'refs/heads/main' }}", text)
        self.assertIn('ref: ${{ forgejo.sha }}', text)
        self.assertIn('CONTROL_EVENT: ${{ forgejo.event_name }}', text)
        self.assertIn('CONTROL_SHA: ${{ forgejo.sha }}', text)
        self.assertIn('persist-credentials: false', text)
        self.assertIn('group: alphapulse-delivery', text)
        self.assertIn('cancel-in-progress: false', text)
        for name in ('release.yml', 'sync-github.yml'):
            sibling = (WORKFLOW.parent / name).read_text()
            self.assertIn('group: alphapulse-delivery', sibling)
            self.assertIn('cancel-in-progress: false', sibling)
        self.assertEqual(set(re.findall(r'secrets\.([A-Z_]+)', text)), {'RELEASE_TOKEN'})
        self.assertIn('if: ${{ always() }}', text)
        self.assertEqual(text.count('/alphapulse-promote-${{ forgejo.run_id }}-${{ forgejo.run_attempt }}'), 2)
        self.assertIn("['cleanup_auth']()", text)
        job_env = text.split('    env:\n', 1)[1].split('    steps:', 1)[0]
        self.assertNotIn('runner.temp', job_env)
        self.assertNotIn('docker.sock', text)
        self.assertNotIn('skopeo', text)
        for forbidden in ('pytest', 'pnpm', 'uv ', 'scripts/manual-release.py', 'scripts/sync-github.py',
                          'start.sh', 'workflow_dispatches', 'inputs:', '--force', 'git checkout dev'):
            self.assertNotIn(forbidden, text)
        self.assertIn('runs-on: docker', text)
        self.assertIn('checkout@11d5960a326750d5838078e36cf38b85af677262', text)


if __name__ == '__main__':
    unittest.main()
