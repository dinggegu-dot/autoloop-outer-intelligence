"""在临时目录运行正式形态的回环网关，只使用合成工单。"""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from live_filename_index import Board, Isolated, loopback_port, wait_until
from prepare_formal_canary import prepare
from prepare_formal_outer_pilot import replace_once, source_only
from run_goal_team_pilot import label_browser_fixture_operator


BUDGET_CASE = "合成工作坊简报 · 授权与续办"


def budget_fixture(app):
    source = app / "superagent/outer_intake.py"
    marker = "                ticket['approval_terms']['execution_profile'] = PROFILE\n"
    text = replace_once(source.read_text(), marker, marker + f"""
                if request.get('name') == {BUDGET_CASE!r}:
                    ticket['approval_terms']['team_role_limit'] = 1
""")
    compile(text, str(source), "exec")
    source.write_text(text)


def start(stack, command, name, root, env):
    with (root / f"{name}.log").open("ab") as output:
        process = subprocess.Popen(command, cwd=root, env=env, start_new_session=True,
                                   stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT)
    stack.callback(Isolated.stop, process)
    return process


def launch(args):
    root = Path(tempfile.mkdtemp(prefix="autoloop-authorized-goal-", dir="/private/tmp"))
    print(f"隔离目录 {root}", flush=True)
    formal = args.formal_source.resolve(strict=True)
    if not formal.is_relative_to(Path("/private/tmp")):
        raise ValueError("须使用正式工作台源码的临时副本")
    codex_home = args.codex_home.resolve(strict=True)
    if not codex_home.is_relative_to(Path("/private/tmp")) or not codex_home.is_dir():
        raise ValueError("须使用已授权的隔离 Codex 配置")
    front_port, side_port, hub_port = [loopback_port() for _ in range(3)]
    if len({front_port, side_port, hub_port, 8765}) != 4:
        raise RuntimeError("隔离端口冲突")
    bundle = root / "bundle"
    prepare(formal, bundle)
    side = root / "side"
    side.mkdir(mode=0o700)
    shutil.move(bundle / "app", side / "app")
    budget_fixture(side / "app")
    label_browser_fixture_operator(side / "app")
    for name in ("formal_outer_canary_supervisor.py", "formal_outer_canary_wire.py"):
        shutil.copy2(Path(__file__).with_name(name), side / name)
    (side / "gateway.key").write_text(secrets.token_hex(32))
    profile = side / "profile.json"
    profile.write_text(json.dumps({"board_port": side_port}))
    front = root / "front"
    shutil.copytree(formal, front, ignore=source_only(formal))
    shutil.copytree(bundle / "front", front, dirs_exist_ok=True)
    env = {**os.environ, "CODEX_HOME": str(codex_home)}
    if args.proxy:
        env["AUTOLOOP_OUTER_PROXY_PORT"] = str(args.proxy)

    with ExitStack() as stack:
        side_proc = start(stack, [sys.executable, "-B", str(side / "formal_outer_canary_supervisor.py"),
                                  "--root", str(side), "--board-port", str(side_port),
                                  "--hub-port", str(hub_port)], "side", root, env)
        def ready():
            if side_proc.poll() is not None:
                raise RuntimeError("隔离侧服务已退出，请查看隔离 side.log")
            return "READY" in (root / "side.log").read_text()
        wait_until(ready, 80, "隔离侧服务未就绪")
        front_proc = start(stack, [sys.executable, "-B", str(front / "server.py"),
                                   "--outer-canary-profile", str(profile), "--port", str(front_port),
                                   "--runtime", str(root / "legacy-ledger")], "front", root, env)
        url = f"http://127.0.0.1:{front_port}"
        wait_until(lambda: Isolated.ready(url + "/api/session"), 20, "隔离工作台未就绪")
        board = Board(front_port)
        if board.call("/api/outer-canary/drafts").get("goal_team_enabled") is not True:
            raise RuntimeError("隔离工作台未接通随包签名网关")
        print(f"隔离工作台 {url}/#tasks", flush=True)
        (root / "instance.json").write_text(json.dumps(
            {"board": url, "side_port": side_port, "hub_port": hub_port,
             "ledger": str(side / "runtime/workbench/tasks.sqlite3")}) + "\n")
        while side_proc.poll() is None and front_proc.poll() is None:
            time.sleep(1)
        raise RuntimeError("隔离服务意外退出")


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--formal-source", required=True, type=Path)
    parser.add_argument("--codex-home", required=True, type=Path)
    parser.add_argument("--proxy", type=int, choices=range(1, 65536), metavar="PORT")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        launch(args)
    except KeyboardInterrupt:
        pass
