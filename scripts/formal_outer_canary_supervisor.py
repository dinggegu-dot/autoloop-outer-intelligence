"""启动并监管正式工作台 canary 的独立 Hub、Worker 和后台。"""

import argparse
import fcntl
import hashlib
import hmac
import http.client
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path

from formal_outer_canary_wire import client_ticket, response_proof


HUB_KEY = re.compile(r'[A-Za-z0-9_-]{43}')
RUNTIME_DIRS = ('hub', 'worker', 'workbench', 'tmp', 'cwd')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def owned(path, *, directory=False, private=True):
    info = path.lstat()
    valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1
    require(valid_type and info.st_uid == os.getuid() and (not private or not info.st_mode & 0o077),
            f'canary 路径类型、所有者或权限无效：{path}')


def read_json(path):
    owned(path)
    return json.loads(path.read_text())


def write_text(path, text):
    with path.open('x') as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def write_json(path, value):
    write_text(path, json.dumps(value, indent=2) + '\n')


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def request(port, path, headers=None):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=0.8)
    try:
        connection.request('GET', path, headers=headers or {})
        response = connection.getresponse()
        if path == '/v1/state' and response.status == 200:
            state = json.load(response)
            return any(agent['online'] and agent['worker'] == 'local'
                       and agent['id'] == 'odyssey-goal' for agent in state['agents'])
        return response.status
    except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError):
        return None
    finally:
        connection.close()


def board_ready(port, key):
    path = '/api/session'
    if request(port, path) != 403:
        return False
    ticket = client_ticket(key, 'GET', path)
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=0.8)
    try:
        connection.request('GET', path, headers={'X-Odyssey-Canary-Gateway': ticket})
        response = connection.getresponse()
        proof = response.getheader('X-Odyssey-Canary-Proof', '')
        if response.status != 200 or not re.fullmatch(r'[0-9a-f]{64}', proof):
            return False
        payload = response.read(4097)
        expected = response_proof(key, ticket.split('.', 1)[0], response.status, payload)
        return len(payload) <= 4096 and hmac.compare_digest(proof, expected)
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


class Supervisor:
    def __init__(self, root, board_port, hub_port):
        owned(root, directory=True)
        self.root = root.resolve(strict=True)
        expected_root = Path.home() / 'Library/Application Support/Autoloop/workbench/outer-canary'
        require(self.root == expected_root or Path('/private/tmp').resolve() in self.root.parents,
                'canary 根目录须使用正式 canary 专用位置或隔离临时目录')
        self.app = self.root / 'app'
        owned(self.app, directory=True, private=False)
        owned(self.app / 'superagent', directory=True, private=False)
        self.board = self.app / 'server.py'
        self.goal = self.app / 'superagent/goal_runtime.py'
        owned(self.board, private=False)
        owned(self.goal, private=False)

        key_file = self.root / 'gateway.key'
        owned(key_file)
        require(stat.S_IMODE(key_file.stat().st_mode) == 0o600, 'gateway.key 权限须为 0600')
        self.gateway_key = key_file.read_text().strip()
        require(re.fullmatch(r'[0-9a-f]{64}', self.gateway_key), 'gateway.key 格式无效')

        self.board_port = board_port
        self.hub_port = hub_port
        self.runtime = self.root / 'runtime'
        self.hub_url = f'http://127.0.0.1:{hub_port}'
        self.manifest = {'version': 1, 'board_port': board_port, 'hub_port': hub_port,
                         'gateway_sha256': hashlib.sha256(self.gateway_key.encode()).hexdigest()}
        install = Path.home() / '.agent-autoloop'
        node = install / 'runtime/node-v24.22.2/bin/node'
        entry = install / 'app/bin/agent-autoloop.ts'
        require(os.access(node, os.X_OK) and entry.is_file(), '找不到已安装的 Autoloop CLI')
        self.cli = [str(node), '--disable-warning=ExperimentalWarning', str(entry)]
        self.env = {key: os.environ[key] for key in ('PATH', 'CODEX_HOME', 'LANG', 'LC_ALL', 'LC_CTYPE')
                    if key in os.environ}
        self.env.update(HOME=str(Path.home()), SHELL='/bin/sh', TMPDIR=str(self.runtime / 'tmp'))
        outer_proxy = os.environ.get('AUTOLOOP_OUTER_PROXY_PORT')
        if outer_proxy:
            require(re.fullmatch(r'[0-9]{1,5}', outer_proxy) and 0 < int(outer_proxy) <= 65535,
                    '模型连接端口无效')
            self.env['AUTOLOOP_OUTER_PROXY_PORT'] = outer_proxy
        self.processes = []
        self.stopping = False

    def rules(self):
        rule = 'odyssey-goal'
        command = [sys.executable, '-B', str(self.goal), 'worker', '--runtime', str(self.runtime / 'workbench'),
                   '--hub', str(self.runtime / 'hub')]
        return {
            'sources': {'local': {'name': 'Local'}}, 'channels': {},
            'agents': {rule: {'name': 'Odyssey Goal', 'worker': 'local', 'use': 'command',
                              'timeout': 6, 'command': command}},
            'rules': [{'name': rule, 'when': {'source': 'local', 'types': ['workbench.goal.assignment']},
                       'to': rule, 'cwd': str(self.runtime / 'cwd'),
                       'prompt': 'Process the Workbench goal assignment.', 'parallel': 1, 'tell': 'never'}],
        }

    def configs(self, owner, source, worker):
        hub = {'url': self.hub_url, 'hub': {'listen': f'127.0.0.1:{self.hub_port}'},
               'owner': owner, 'source': {'name': 'local', 'key': source}}
        worker = {'url': self.hub_url, 'worker': {'name': 'local', 'key': worker, 'capacity': 1}}
        return hub, worker

    def initialize(self):
        # 中台密钥和数据库一起安装，首次中断时可在新目录重试。
        with tempfile.TemporaryDirectory(prefix='.runtime-', dir=self.root) as temporary:
            staged = Path(temporary) / 'runtime'
            staged.mkdir()
            for name in RUNTIME_DIRS:
                (staged / name).mkdir()
            write_json(staged / 'hub/rules.json', self.rules())
            env = dict(self.env, AGENT_AUTOLOOP_HOME=str(staged / 'hub'), TMPDIR=str(staged / 'tmp'))

            def cli(*args):
                if self.stopping:
                    raise InterruptedError
                result = subprocess.run([*self.cli, *args], env=env, cwd=staged / 'cwd',
                                        capture_output=True, text=True, timeout=10)
                require(result.returncode == 0, f'独立 Autoloop 初始化失败：{args[0]}')
                return result.stdout.strip()

            cli('check')
            keys = [cli('key', 'new', kind, name) for kind, name in
                    (('owner', 'owner'), ('source', 'local'), ('worker', 'local'))]
            require(all(HUB_KEY.fullmatch(key) for key in keys), '独立 Autoloop 返回的密钥格式无效')
            hub, worker = self.configs(*keys)
            write_json(staged / 'hub/config.json', hub)
            write_json(staged / 'worker/config.json', worker)
            write_json(staged / 'supervisor.json', self.manifest)
            write_text(staged / '.goal-pilot', f'{self.hub_port}\n')
            for name in RUNTIME_DIRS:
                sync_directory(staged / name)
            sync_directory(staged)
            if self.stopping:
                raise InterruptedError
            staged.rename(self.runtime)
            sync_directory(self.root)

    def validate_runtime(self):
        owned(self.runtime, directory=True)
        for name in RUNTIME_DIRS:
            owned(self.runtime / name, directory=True)
        marker = self.runtime / '.goal-pilot'
        owned(marker)
        require(read_json(self.runtime / 'supervisor.json') == self.manifest
                and marker.read_text() == f'{self.hub_port}\n', 'canary 恢复参数与初始化记录不一致')
        require(read_json(self.runtime / 'hub/rules.json') == self.rules(), 'canary 中台规则已变化')
        hub = read_json(self.runtime / 'hub/config.json')
        worker = read_json(self.runtime / 'worker/config.json')
        keys = (hub['owner'], hub['source']['key'], worker['worker']['key'])
        require(all(isinstance(key, str) and HUB_KEY.fullmatch(key) for key in keys)
                and (hub, worker) == self.configs(*keys), 'canary 中台或 Worker 配置已变化')
        owned(self.runtime / 'hub/hub.db')
        for path in ('hub/hub.db-wal', 'hub/hub.db-shm', 'workbench/tasks.sqlite3',
                     'workbench/tasks.sqlite3-wal', 'workbench/tasks.sqlite3-shm'):
            item = self.runtime / path
            if item.exists() or item.is_symlink():
                owned(item)
        secrets_file = self.runtime / 'hub/secrets.env'
        require(not secrets_file.exists() and not secrets_file.is_symlink(), 'canary 中台不需要 secrets.env')
        return keys[0]

    def start(self, name, command, env):
        log_path = self.runtime / f'{name}.log'
        fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'ab') as log:
            owned(log_path)
            cursor = log.tell()
            process = subprocess.Popen(command, cwd=self.runtime / 'cwd', env=env, start_new_session=True,
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        self.processes.append((name, process))

        def logged(line):
            with log_path.open('rb') as handle:
                handle.seek(cursor)
                return line.encode() in handle.read()

        return logged

    def alive(self):
        if self.stopping:
            raise InterruptedError
        for name, process in self.processes:
            require(process.poll() is None, f'{name} 已退出（{process.returncode}），详见 runtime/{name}.log')

    def wait_ready(self, check, name):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            self.alive()
            if check():
                return
            time.sleep(0.2)
        raise ValueError(f'{name} 就绪超时，详见 runtime/{name}.log')

    def scheduler_running(self):
        lock = self.runtime / 'workbench/goal-runtime.lock'
        if not lock.exists():
            return False
        with lock.open('r') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
        return False

    def run(self):
        if not self.runtime.exists() and not self.runtime.is_symlink():
            self.initialize()
        owner = self.validate_runtime()
        self.alive()
        hub_log = self.start('hub', [*self.cli, 'up'], dict(self.env, AGENT_AUTOLOOP_HOME=str(self.runtime / 'hub')))
        self.wait_ready(lambda: hub_log(f'[hub] listening on 127.0.0.1:{self.hub_port}')
                        and request(self.hub_port, '/healthz') == 200, 'hub')
        self.start('worker', [*self.cli, 'up'], dict(self.env, AGENT_AUTOLOOP_HOME=str(self.runtime / 'worker')))
        self.wait_ready(lambda: request(self.hub_port, '/v1/state', {'Authorization': f'Bearer {owner}'}) is True,
                        'worker')
        board_log = self.start('board', [sys.executable, '-B', str(self.board), '--goal-pilot',
                               '--outer-intelligence', '--outer-workbench-entry', '--outer-model-timeout', '120',
                               '--runtime', str(self.runtime / 'workbench'), '--hub-home', str(self.runtime / 'hub'),
                               '--port', str(self.board_port)], dict(self.env, AUTOLOOP_CANARY_GATEWAY_KEY=self.gateway_key))
        self.wait_ready(lambda: board_log(f'Local: http://127.0.0.1:{self.board_port}')
                        and board_ready(self.board_port, self.gateway_key), 'board')
        require(not self.scheduler_running(), '已有其他目标调度器持有灰度账本')
        self.start('scheduler', [sys.executable, '-B', str(self.goal), 'serve',
                                '--runtime', str(self.runtime / 'workbench'), '--hub', str(self.runtime / 'hub')], self.env)
        self.wait_ready(self.scheduler_running, 'scheduler')
        self.alive()
        print('READY', flush=True)
        while True:
            self.alive()
            time.sleep(0.25)

    def stop(self):
        processes = [process for _, process in reversed(self.processes)]
        for process in processes:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        for process in processes:
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--board-port', required=True, type=int)
    parser.add_argument('--hub-port', required=True, type=int)
    args = parser.parse_args()
    ports = {args.board_port, args.hub_port}
    if not args.root.is_absolute() or len(ports) != 2 or 8765 in ports or not all(0 < port <= 65535 for port in ports):
        parser.error('须指定绝对根目录以及两个不同于 8765 的有效端口')
    os.umask(0o077)
    try:
        supervisor = Supervisor(args.root, args.board_port, args.hub_port)
        lock = supervisor.root / 'supervisor.lock'
        with os.fdopen(os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600), 'w') as handle:
            owned(lock)
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

            def terminate(signum, frame):
                supervisor.stopping = True

            signal.signal(signal.SIGTERM, terminate)
            signal.signal(signal.SIGINT, terminate)
            try:
                supervisor.run()
            except InterruptedError:
                if not supervisor.stopping:
                    raise
            finally:
                supervisor.stop()
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        # 配置异常和初始化超时仅打印固定诊断，避免回显中台凭据。
        print(error if type(error) is ValueError else f'canary 启动失败（{type(error).__name__}）', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
