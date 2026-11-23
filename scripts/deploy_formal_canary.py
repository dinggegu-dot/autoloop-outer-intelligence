"""安装或回退本机 8765 的合成文件名索引灰度。"""
import argparse
from contextlib import closing, contextmanager
import errno
import hashlib
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import plistlib
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.request import HTTPCookieProcessor, ProxyHandler, build_opener

from prepare_formal_canary import FRONT_FILES, prepare


ROOT = Path.home() / "Library/Application Support/Autoloop/workbench"
APP, STATE, CANARY = (ROOT / name for name in ("app", "state", "outer-canary"))
LAUNCH = Path.home() / "Library/LaunchAgents"
BOARD = "local.autoloop.odyssey-workbench"
SIDE = "local.autoloop.workbench-outer-canary"
DOMAIN = f"gui/{os.getuid()}"
BOARD_PLIST, SIDE_PLIST = [LAUNCH / (label + ".plist") for label in (BOARD, SIDE)]
ADDITIONS = ("outer_canary.py", "formal_outer_canary_wire.py", "outer-canary")
DATABASES = ("tasks.sqlite3", "afr-topics.sqlite3", "research-admissions.sqlite3")
ACTIVE = ("queued", "running", "working", "stopping", "planning", "executing", "reviewing", "verifying")
BASE_URL = "http://127.0.0.1:8765"
PLIST_BACKUP = "workbench-launchagent.plist"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def launch(action, detail, *, checked=True):
    command = ["/bin/launchctl", action, DOMAIN, str(detail)] if action == "bootstrap" else [
        "/bin/launchctl", action, f"{DOMAIN}/{detail}"]
    result = subprocess.run(command, capture_output=True, timeout=18)
    if checked:
        require(result.returncode == 0, f"LaunchAgent {action} 失败：{Path(detail).name}")
    return result.returncode == 0


def wait_check(check, reason, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(.3)
    raise RuntimeError(reason)


def read_legacy():
    opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))

    def fetch(path):
        with opener.open(BASE_URL + path, timeout=5) as response:
            return response.read()

    def get(path):
        return json.loads(fetch(path))

    require(get("/api/session").get("csrf"), "正式会话未建立")
    data = get("/api/tasks")
    require(data["capability"]["ledger_available"], "现役工单账本不可用")
    require(type(get("/api/library/archives")) is dict and
            type(get("/api/library/afr-materials")["items"]) is list, "知识库入口不可用")
    articles = get("/api/afr-handoff/library?kind=article&q=&offset=0")["items"]
    questions = get("/api/afr-handoff/library?kind=question&q=&offset=0")["items"]
    require(type(articles) is list and type(questions) is list, "AFR 目录不可用")
    if articles:
        from urllib.parse import urlencode
        keys = {k: articles[0][k] for k in ("kind", "id", "revision")}
        item = get("/api/afr-handoff/item?" + urlencode(keys))["item"]
        require(all(item.get(k) == value for k, value in keys.items()), "AFR 固定版本无法读取")
    known = {task["id"] for task in data["tasks"]}
    if known:
        from urllib.parse import quote
        focus = sorted(known)[0]
        require(focus in {t["id"] for t in get("/api/tasks?task=" + quote(focus))["tasks"]},
                "正式旧工单详情读取失败")
    return fetch, get, known


@contextmanager
def sqlite_source(path, *, live):
    # macOS 上现役 WAL 连接可能需要创建共享内存。查询连接禁止修改账本。
    wal = path.with_name(path.name + "-wal")
    query = "mode=rw" if live or (wal.is_file() and wal.stat().st_size > 0) else "immutable=1"
    with closing(sqlite3.connect(path.as_uri() + "?" + query, uri=True, timeout=4)) as db:
        db.execute("PRAGMA query_only=ON")
        yield db


def read_state(*, backup=None, idle=False, live=True):
    task_ids = set()
    for name in DATABASES:
        path = STATE / name
        if not path.exists() and name != "tasks.sqlite3":
            continue
        require(not path.is_symlink(), "正式数据不能通过链接读取")
        with sqlite_source(path, live=live) as db:
            require(db.execute("PRAGMA quick_check").fetchone() == ("ok",), "正式数据库完整性检查失败")
            if name == "tasks.sqlite3":
                task_ids = {row[0] for row in db.execute("SELECT id FROM tasks")}
                if idle:
                    status = "json_extract(body,'$.status')"
                    marks = ",".join("?" for _ in ACTIVE)
                    active, = db.execute(f"""SELECT COUNT(*) FROM tasks WHERE {status} IN ({marks})
                        OR json_type(body,'$.active_call') NOT IN ('null')""", ACTIVE).fetchone()
                    require(active == 0, "有活动的正式工单，保留原工作台继续处理")
            elif name == "afr-topics.sqlite3" and idle:
                running, = db.execute("SELECT COUNT(*) FROM afr_topics_v1 WHERE status='running'").fetchone()
                require(running == 0, "有运行中的 AFR 任务，保留原工作台继续处理")
            if backup is not None:
                with closing(sqlite3.connect(backup / name)) as replica:
                    db.backup(replica)
                    require(replica.execute("PRAGMA quick_check").fetchone() == ("ok",), "正式账本备份完整性检查失败")
    return task_ids


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic(path, data, mode):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
            temporary.replace(path)
            sync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)


def ports():
    with socket.socket() as first, socket.socket() as second:
        first.bind(("127.0.0.1", 0))
        second.bind(("127.0.0.1", 0))
        result = (first.getsockname()[1], second.getsockname()[1])
    require(8765 not in result, "灰度使用的端口与正式端口冲突")
    return result


def select_python(plist):
    python, *args = plist["ProgramArguments"]
    require(plist.get("Label") == BOARD and plist.get("Program", python) == python
            and str(APP / "server.py") in args
            and args[args.index("--port") + 1] == "8765"
            and args[args.index("--runtime") + 1] == str(STATE)
            and "--outer-canary-profile" not in args, "正式启动参数已变，需重新核对")
    require(Path(python).is_file() and os.access(python, os.X_OK), "正式 Python 解释器不可用")
    return python


def start_front():
    wait_check(lambda: launch("bootstrap", BOARD_PLIST, checked=False)
               or launch("print", BOARD, checked=False), "正式工作台 LaunchAgent 未重新加载")
    wait_check(read_legacy, "正式工作台或原 AFR 路线未恢复")


def stop_front():
    if launch("print", BOARD, checked=False):
        requested = launch("bootout", BOARD, checked=False)
        require(requested or not launch("print", BOARD, checked=False), "正式工作台 LaunchAgent 未能停止")

    def offline():
        if launch("print", BOARD, checked=False):
            return False
        with socket.socket() as listener:
            listener.settimeout(.3)
            return listener.connect_ex(("127.0.0.1", 8765)) == errno.ECONNREFUSED

    wait_check(offline, "正式工作台未停止，保留现有入口文件")


def read_recovery(backup):
    expected_root = (STATE / "deploy-before").resolve()
    require(backup.parent.resolve() == expected_root and backup.name.startswith("outer-canary-")
            and backup.is_dir() and not backup.is_symlink(), "指定的正式恢复点无效")
    marker = backup / "recovery.json"
    durable = marker.exists() or marker.is_symlink()
    if not durable:
        # 兼容已经上线的旧版安装器所留下的恢复点。
        marker = backup / "deployment.json"
    require(marker.is_file() and not marker.is_symlink(), "缺少有效的正式恢复清单")
    manifest = json.loads(marker.read_bytes())
    require(manifest.get("port") == 8765, "恢复清单的正式端口不一致")
    expected = manifest["legacy_source_sha256"]
    require(set(expected) == set(FRONT_FILES), "恢复清单的原入口文件不完整")
    if durable:
        require(manifest.get("version") == 1, "正式恢复清单版本无效")
        expected[PLIST_BACKUP] = manifest["legacy_launchagent_sha256"]
    originals = {}
    for name in (*FRONT_FILES, PLIST_BACKUP):
        source = backup / name
        require(source.is_file() and not source.is_symlink(), f"缺少有效的正式备份：{name}")
        data = source.read_bytes()
        if name in expected:
            require(hashlib.sha256(data).hexdigest() == expected[name], f"正式备份摘要不符：{name}")
        originals[name] = (data, source.stat().st_mode & 0o777)
    select_python(plistlib.loads(originals[PLIST_BACKUP][0]))
    return originals


def restore_front(backup, originals):
    for name in FRONT_FILES:
        atomic(APP / name, *originals[name])
    atomic(BOARD_PLIST, *originals[PLIST_BACKUP])
    for name in ADDITIONS:
        generated = APP / name
        target = backup / ("disabled-" + name)
        if generated.exists() and not target.exists():
            generated.rename(target)


def install():
    require(launch("print", BOARD) and not launch("print", SIDE, checked=False), "工作台 LaunchAgent 状态异常")
    require(not CANARY.exists() and not SIDE_PLIST.exists() and all(not (APP / n).exists() for n in ADDITIONS),
            "发现现有灰度服务目录，先核对已有部署或从恢复点回退")
    plist_bytes = BOARD_PLIST.read_bytes()
    plist = plistlib.loads(plist_bytes)
    python = select_python(plist)
    _, _, known = read_legacy()
    ids_before = read_state(idle=True)
    supervisor = Path(__file__).with_name("formal_outer_canary_supervisor.py")
    wire = supervisor.with_name("formal_outer_canary_wire.py")
    candidate = subprocess.check_output(["git", "-C", str(supervisor.parent.parent), "rev-parse", "HEAD"], text=True).strip()
    codex = shutil.which("codex")
    require(codex, "找不到现有 Codex CLI")
    recovery_dir = STATE / "deploy-before"
    recovery_dir.mkdir(exist_ok=True)
    sync_directory(STATE)
    backup = Path(tempfile.mkdtemp(prefix="outer-canary-", dir=recovery_dir))
    sync_directory(recovery_dir)
    with tempfile.TemporaryDirectory(prefix="autoloop-outer-deployment-") as staging:
        bundle = Path(staging) / "build"
        for name in FRONT_FILES:
            original = APP / name
            require(original.is_file() and not original.is_symlink(), f"正式入口文件不可用：{name}")
            atomic(backup / name, original.read_bytes(), original.stat().st_mode & 0o777)
        atomic(backup / PLIST_BACKUP, plist_bytes, 0o600)
        prepare(backup, bundle)
        baseline = json.loads((bundle / "baseline.json").read_text())
        recovery = {"version": 1, "candidate": candidate, "port": 8765, "legacy_source_sha256": baseline,
                    "legacy_launchagent_sha256": sha(backup / PLIST_BACKUP)}
        atomic(backup / "recovery.json", (json.dumps(recovery, indent=2) + "\n").encode(), 0o600)
        originals = read_recovery(backup)
        print(f"正式恢复点 {backup}", flush=True)
        stopped = overlay_started = side_installed = False
        try:
            CANARY.mkdir(mode=0o700)
            shutil.copytree(bundle / "app", CANARY / "app")
            for source in (supervisor, wire):
                shutil.copy2(source, CANARY / source.name)
            atomic(CANARY / "gateway.key", secrets.token_hex(32).encode(), 0o600)
            board_port, hub_port = ports()
            profile = CANARY / "profile.json"
            atomic(profile, json.dumps({"board_port": board_port}).encode(), 0o600)
            supervisor_log = CANARY / "supervisor.log"
            side = dict(Label=SIDE, RunAtLoad=True, KeepAlive=True, ThrottleInterval=5, Umask=0o077,
                        ProgramArguments=[python, "-B", str(CANARY / supervisor.name), "--root", str(CANARY),
                                          "--board-port", str(board_port), "--hub-port", str(hub_port)],
                        EnvironmentVariables={"PATH": str(Path(codex).parent) + ":/usr/bin:/bin:/usr/sbin:/sbin"},
                        WorkingDirectory=str(CANARY), StandardOutPath=str(supervisor_log),
                        StandardErrorPath=str(supervisor_log))
            atomic(SIDE_PLIST, plistlib.dumps(side), 0o600)
            side_installed = True
            launch("bootstrap", SIDE_PLIST)
            wait_check(lambda: supervisor_log.is_file() and "READY" in supervisor_log.read_text(),
                       "独立灰度服务未就绪", 40)
            read_state(idle=True)
            stopped = True
            stop_front()
            ids_stopped = read_state(backup=backup, idle=True, live=False)
            require(ids_before <= ids_stopped, "部署前后正式工单集合不一致")
            require(BOARD_PLIST.read_bytes() == plist_bytes and all(sha(APP / name) == baseline[name] for name in FRONT_FILES),
                    "正式入口文件或启动配置在构建期间已变化")
            overlay_started = True
            for name in FRONT_FILES:
                p = APP / name
                require(sha(p) == baseline[name], "正式源码在切换期间已变化")
                atomic(p, (bundle / "front" / name).read_bytes(), p.stat().st_mode & 0o777)
            for name in ADDITIONS:
                source = bundle / "front" / name
                if source.is_dir():
                    shutil.copytree(source, APP / name)
                else:
                    atomic(APP / name, source.read_bytes(), 0o644)
            plist["ProgramArguments"].extend(["--outer-canary-profile", str(profile)])
            atomic(BOARD_PLIST, plistlib.dumps(plist), 0o600)
            start_front()
            stopped = False
            fetch, get, observed = read_legacy()
            require(known <= observed and ids_stopped <= read_state(), "切换后出现旧工单缺失")
            require(b'id="outer-canary-submit"' in fetch("/") and b'/outer-canary/app.js' in fetch("/workbench"),
                    "正式页面没有上线灰度入口")
            drafts = get("/api/outer-canary/drafts")
            require(drafts["enabled"] is True and isinstance(drafts["drafts"], list), "新灰度入口账本状态异常")
            manifest = {
                "candidate": candidate, "port": 8765, "legacy_tickets_preserved": len(ids_stopped),
                "legacy_database_backups": [n for n in DATABASES if (backup / n).is_file()],
                "legacy_source_sha256": baseline,
                "installed_overlay_sha256": {
                    str(p.relative_to(bundle / "front")): sha(p) for p in sorted((bundle / "front").rglob("*")) if p.is_file()
                },
                "gate": "fixed-synthetic-filename-index-v2",
            }
            require(all(sha(APP / name) == fingerprint for name, fingerprint in manifest["installed_overlay_sha256"].items()),
                    "安装文件与构建产物不一致")
            atomic(backup / "deployment.json", (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(), 0o600)
            print(json.dumps(manifest, ensure_ascii=False), flush=True)
        except BaseException:
            if overlay_started:
                stop_front()
                restore_front(backup, originals)
                start_front()
            elif stopped:
                start_front()
            if side_installed:
                if launch("print", SIDE, checked=False):
                    launch("bootout", SIDE)
                SIDE_PLIST.rename(backup / "side-disabled.plist")
            print("已保留恢复点以及灰度目录。", flush=True)
            raise


def rollback(backup):
    originals = read_recovery(backup)
    if launch("print", BOARD, checked=False):
        read_state(idle=True)
    stop_front()
    restore_front(backup, originals)
    start_front()
    # 侧服务保留原账本。仍在执行的单可以先排空，再由运维关闭对应 LaunchAgent。
    print("正式入口已回退，现役数据和灰度账本保存在原位置。", flush=True)


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--preflight", action="store_true")
    operation.add_argument("--install", action="store_true")
    operation.add_argument("--rollback", type=Path)
    args = parser.parse_args()
    if args.preflight:
        _, _, known = read_legacy()
        ids = read_state(idle=True)
        print(f"正式旧单、现役 AFR 和数据库检查通过。账本 {len(ids)} 张，前台 {len(known)} 张。")
    elif args.install:
        install()
    else:
        rollback(args.rollback.absolute())
