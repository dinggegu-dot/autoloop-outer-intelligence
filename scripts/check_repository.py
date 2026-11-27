"""检查可达 Git 对象和工程需求引用，不输出命中内容。"""
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BLOCKED_PATH = re.compile(r'(^|/)(\.runtime|\.env(?:\..*)?|验收证据|private\.json|settings\.json|secrets\.env|config\.local\.json|master\.key)(/|$)|\.(?:key|pem|db|sqlite3?)(?:-wal|-shm)?$')
# 经审查的界面素材和合成工单截图按路径与摘要登记。
AUDITED_IMAGES = {
    'docs/assets/autoloop-workbench.png': '8c9135cf7af4d3d50a32c9edf71f49918d4b6729c7a184b0eba08573b2a833f3',
    'scripts/assets/odyssey-team/bowser.png': '86bb10f2a62cce0c9e05a7717fac976636a51c88a0d6176a704b843067c09e44',
    'scripts/assets/odyssey-team/peach.png': '4d7e31f365ae31daa79fbd9b6468ce90e3002f46ad7367cb854e576eadfe9feb',
}
CREDENTIALS = [
    re.compile(rb'(?:gh[pousr]_|github_pat_|sk-proj-|xox[baprs]-)[A-Za-z0-9_-]{16,}'),
    re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    re.compile(rb'(?:ou_|om_|cli_)[a-fA-F0-9]{24,}'),
    re.compile(rb'(?i)https?://[^\s/:@]+:[^\s/@]+@'),
]

def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT)

def check_file(name, data):
    if BLOCKED_PATH.search(name) or data.startswith(b'SQLite format 3\x00'):
        raise ValueError('禁止导入运行资料：' + name)
    if len(data) > 2_000_000 or b'\0' in data:
        if AUDITED_IMAGES.get(name) != hashlib.sha256(data).hexdigest():
            raise ValueError('需要单独审查二进制文件：' + name)
    if any(pattern.search(data) for pattern in CREDENTIALS):
        raise ValueError('发现疑似私人标识或凭据：' + name)

def main():
    examined = set()
    trees = git('rev-list', '--all').decode().splitlines()
    for rev in trees:
        entries = git('ls-tree', '-rz', rev).split(b'\x00')
        for entry in filter(None, entries):
            metadata, raw_name = entry.split(b'\t', 1)
            mode, kind, oid = metadata.split()
            name = raw_name.decode()
            if kind != b'blob' or mode not in (b'100644', b'100755'):
                raise ValueError('需要审查特殊 Git 对象：' + name)
            if (name, oid) not in examined:
                check_file(name, git('cat-file', 'blob', oid.decode()))
                examined.add((name, oid))
    for raw in filter(None, git('ls-files','-z').split(b'\0')):
        name = raw.decode()
        body = (ROOT / name).read_bytes()
        check_file(name, body)
        if name.endswith('.py'):
            ast.parse(body, filename=name)
        if name.endswith('.json'):
            json.loads(body)
    prd = (ROOT/'docs/product/PRD-v2-outer-loop.md').read_text()
    matrix = (ROOT/'docs/testing/acceptance-matrix.md').read_text()
    for i in range(1, 13):
        fr = 'FR-%02d' % i
        if fr not in prd or fr not in matrix:
            raise ValueError('需求遗漏：' + fr)
    print(f'检查 {len(trees)} 个可达提交、{len(examined)} 个文件版本、12 项需求。')

if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
