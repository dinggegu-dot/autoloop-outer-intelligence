"""只读 MCP 桥。运行中的 Worker 保管证据，Codex 只持有本轮的本地套接字。"""
import hashlib
import json
import socket
import socketserver
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

SERVER = 'autoloop_ticket'
DESCRIPTIONS = {
    'read_failure_evidence': '读取本单触发协调的失败证据及历史失败。',
    'read_verified_artifacts': '读取本单已独立验过的产物版本和可保留的产物。',
    'read_plan_versions': '读取本单的计划版本、受影响步骤和验收项。',
    'read_remaining_budget': '读取本单当前授权版本和剩余预算。',
}
_LIMIT = 64_000


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode()


class _Request(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(1)
        try:
            raw = self.rfile.readline(256)
            name = raw.decode('ascii').strip()
            if name not in DESCRIPTIONS or not raw.endswith(b'\n'):
                return
            with self.server.launch_gate():
                value = self.server.evidence[name]
                payload = _encode(value)
                self.wfile.write(b'{"value":' + payload + b'}\n')
                self.server.completed[name] = hashlib.sha256(payload).hexdigest()
        except Exception:
            # 被撤销的授权、暂停或传输错误都不返回任何本地细节。
            try:
                self.wfile.write(b'{"denied":true}\n')
            except OSError:
                pass


@contextmanager
def serve(evidence, launch_gate):
    if not isinstance(evidence, dict) or evidence.keys() != DESCRIPTIONS.keys():
        raise ValueError('invalid coordination evidence')
    if len(_encode(evidence)) > _LIMIT:
        raise ValueError('coordination evidence exceeds its limit')
    # macOS 的 UNIX 套接字地址长度有限，使用短路径和仅本人可进入的目录。
    with tempfile.TemporaryDirectory(prefix='autoloop-mcp-', dir='/tmp') as directory:
        endpoint = str(Path(directory, 'e'))
        with socketserver.UnixStreamServer(endpoint, _Request) as server:
            server.evidence = evidence
            server.launch_gate = launch_gate
            server.completed = {}
            runner = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05})
            runner.start()
            try:
                yield endpoint, server.completed
            finally:
                server.shutdown()
                runner.join()


def _read(endpoint, name):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(3)
        client.connect(endpoint)
        client.sendall(name.encode('ascii') + b'\n')
        with client.makefile('rb') as response:
            raw = response.readline(_LIMIT + 32)
    data = json.loads(raw)
    if 'value' not in data:
        raise ValueError('evidence unavailable')
    return _encode(data['value']).decode()


def _handle(request, endpoint):
    method = request.get('method')
    params = request.get('params') or {}
    if method == 'initialize':
        version = params.get('protocolVersion')
        if version not in ('2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25'):
            version = '2025-11-25'
        return {'protocolVersion': version, 'capabilities': {'tools': {}},
                'serverInfo': {'name': SERVER, 'version': '1'}}
    if method == 'ping':
        return {}
    if method == 'tools/list':
        return {'tools': [
            {'name': name, 'description': description,
             'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
             'annotations': {'readOnlyHint': True, 'openWorldHint': False}}
            for name, description in DESCRIPTIONS.items()
        ]}
    if method == 'tools/call':
        name = params.get('name')
        try:
            if not isinstance(name, str) or name not in DESCRIPTIONS or params.get('arguments', {}) != {}:
                raise ValueError('invalid evidence request')
            message = _read(endpoint, name)
            return {'content': [{'type': 'text', 'text': message}]}
        except (ValueError, OSError):
            return {'isError': True, 'content': [{'type': 'text', 'text': '当前派工的该项证据不可读取。'}]}
    return None


def main(endpoint):
    for raw in sys.stdin.buffer:
        try:
            request = json.loads(raw)
            if not isinstance(request, dict) or 'id' not in request:
                continue
            result = _handle(request, endpoint)
            response = {'jsonrpc': '2.0', 'id': request['id']}
            if result is None:
                response['error'] = {'code': -32601, 'message': 'Method not found'}
            else:
                response['result'] = result
            sys.stdout.buffer.write(_encode(response) + b'\n')
            sys.stdout.buffer.flush()
        except (AttributeError, ValueError):
            continue


if __name__ == '__main__':
    main(sys.argv[1])
