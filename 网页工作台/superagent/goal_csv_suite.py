"""Run the delivered CSV unittest suite in a writable, isolated macOS copy."""
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic

from .goal_checks import _policy
from .goal_team_files import _manifest, _read


_DISCOVER = """
import os
import sys
import unittest

os.environ['TMPDIR'] = os.getcwd()
output, sys.stdout = sys.stdout, sys.stderr
program = unittest.main(module=None, argv=['unittest', 'discover', '-p', 'test*.py'], exit=False)
result = program.result
if not result.wasSuccessful() or result.testsRun <= len(result.skipped):
    sys.exit(1)
print(result.testsRun, len(result.skipped), file=output, flush=True)
"""


def _run_suite(stage, python, profile, policy):
    # 子 Python 继承 Seatbelt。父进程按整个进程组收尾，即使测试主进程先退出。
    argv = ['/usr/bin/sandbox-exec', '-p', profile, python, '-I', '-S', '-B', '-c', _DISCOVER]
    env = {'HOME': str(stage), 'TMPDIR': str(stage), 'LANG': 'en_US.UTF-8',
           'PATH': str(Path(python).parent), 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1'}
    with subprocess.Popen(argv, cwd=stage, env=env, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          start_new_session=True) as child, selectors.DefaultSelector() as streams:
        stdout = bytearray()
        size = 0
        deadline = monotonic() + policy.TIMEOUT
        try:
            streams.register(child.stdout, selectors.EVENT_READ)
            streams.register(child.stderr, selectors.EVENT_READ)
            while streams.get_map():
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError('unittest timed out')
                for key, _ in streams.select(remaining):
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        streams.unregister(key.fileobj)
                        continue
                    size += len(chunk)
                    if size > policy.OUTPUT_LIMIT:
                        raise ValueError('unittest exceeded the output limit')
                    if key.fileobj is child.stdout:
                        stdout.extend(chunk)
            code = child.wait(timeout=max(0, deadline - monotonic()))
        finally:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if code:
            raise ValueError('unittest failed or ran no non-skipped tests')
        counts = stdout.split()
        if len(counts) != 2 or any(not n.isdigit() or len(n) > 9 for n in counts):
            raise ValueError('missing unittest completion result')
        total, skipped = map(int, counts)
        if total <= skipped:
            raise ValueError('unittest ran no non-skipped tests')
        return total, skipped


def verify_suite(workspace: Path) -> dict:
    """Return one fail-closed delivered_tests check without running code in workspace."""
    check = {'id': 'delivered_tests', 'passed': False, 'detail': ''}
    try:
        files = _read(workspace)
        original = _manifest(files)
        inputs = {name: data for name, data in files.items() if name.endswith(('.py', '.csv'))}
        if 'csv_check.py' not in inputs:
            raise ValueError('missing csv_check.py')
        policy = _policy()
        with TemporaryDirectory(prefix='goal-csv-suite-', dir='/private/tmp') as directory:
            root = Path(directory)
            stage = root / 'workspace'
            stage.mkdir(mode=0o700)
            for name, data in inputs.items():
                target = stage / name
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                target.write_bytes(data)
            before = _manifest(_read(stage))
            if before != _manifest(inputs):
                raise ValueError('staged input hash mismatch')
            python, profile = policy._seatbelt(stage)
            path = json.dumps(str(stage))
            profile += f'\n(allow process-fork)\n(allow file-read-data file-write* (subpath {path}))\n'
            profile += f'(deny file-read* (subpath {json.dumps(str(workspace.resolve(strict=True)))}))\n'
            # 在启动任何交付代码前，核实原脚本不可读、副本外不可写、网络被拒。
            probe = policy._run(stage, python, profile, [
                '-c', policy.PROBE, str(workspace.absolute() / 'csv_check.py'), str(root / 'outside')
            ])
            if probe.strip() != b'seatbelt-ok':
                raise ValueError('Seatbelt isolation probe failed')
            total, skipped = _run_suite(stage, python, profile, policy)
            after = _manifest(_read(stage))
            if any(after.get(name) != digest for name, digest in before.items()):
                raise ValueError('staged input or script changed')
            if _manifest(_read(workspace)) != original:
                raise ValueError('original workspace changed')
        check.update(passed=True, detail=f'unittest ran {total} tests ({skipped} skipped); SHA-256 unchanged')
    except Exception as exc:
        # 不外传交付代码的 stderr、环境或本机路径。
        check['detail'] = str(exc)[:200] if isinstance(exc, (ValueError, TimeoutError)) else type(exc).__name__
    return check
