"""跟踪本次模型进程及已观察到的后代，包含 DSH 的 detached 工具进程。

macOS 观察式进程树不是强容器隔离；发现仍活着的后代时不得报告已停。
"""
import contextlib
import os
import signal
import subprocess
import time


def processes():
    output = subprocess.check_output(['/bin/ps', '-ax', '-o', 'pid=', '-o', 'ppid=', '-o', 'pgid=', '-o', 'lstart=', '-o', 'stat='], text=True)
    rows = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 9:
            pid, parent, group = map(int, parts[:3])
            rows[pid] = {'pid': pid, 'parent': parent, 'group': group, 'birth': ' '.join(parts[3:8]), 'state': parts[8]}
    return rows


def alive(saved, current):
    return [row for row in saved if row['pid'] in current and current[row['pid']]['birth'] == row['birth'] and not current[row['pid']]['state'].startswith('Z')]


class ProcessTree:
    def __init__(self, root):
        self.root = root
        self.known = {}
        self.sample()  # 子进程仍在启动握手等待，先固定身份再放行。

    def sample(self):
        current = processes()
        # 已退出的父进程，其此前观察到的后代仍应被跟踪。
        owned = {r['pid'] for r in alive(list(self.known.values()), current)}
        if self.root not in self.known and self.root in current:owned.add(self.root)
        while True:
            added = {pid for pid, row in current.items() if row['parent'] in owned}
            if added <= owned:
                break
            owned |= added
        for pid in owned:
            if pid in current:
                row = current[pid]
                if pid == self.root or row['group'] != os.getpgrp():
                    self.known[pid] = row
        return list(self.known.values())

    def stop(self, child, grace=5.5):
        self.sample()
        if any(row['pid']==self.root for row in alive(list(self.known.values()), processes())):
            with contextlib.suppress(ProcessLookupError):os.kill(self.root, signal.SIGTERM)
        end = time.monotonic() + grace
        while child.poll() is None and time.monotonic() < end:
            self.sample()
            time.sleep(.05)
        self.sample()
        # 只向匹配进程出生时间的本次后代发送信号，避免 PID 重用误伤。
        for row in alive(list(self.known.values()), processes()):
            with contextlib.suppress(ProcessLookupError):
                os.kill(row['pid'], signal.SIGKILL)
        # 主进程已退出时不再按旧 PGID 发信号，避免编号重用误伤。
        if any(row['pid']==self.root for row in alive(list(self.known.values()), processes())):
            with contextlib.suppress(ProcessLookupError):os.killpg(self.root, signal.SIGKILL)
        child.wait(timeout=5)
        end = time.monotonic() + 5
        while True:
            self.sample()
            live = alive(list(self.known.values()), processes())
            if not live:
                return
            if time.monotonic() >= end:
                raise RuntimeError('模型工具进程尚未确认退出，禁止继续新执行')
            for row in live:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(row['pid'], signal.SIGKILL)
            time.sleep(.05)
