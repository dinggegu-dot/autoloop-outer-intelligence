"""安装、升级或回退本机 8765 的固定合成工单灰度。"""
import argparse
from contextlib import closing, contextmanager
import errno
import fcntl
import hashlib
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shutil
import socket
import sqlite3
import stat
import subprocess
import tarfile
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
SIDE_FILES = ("app", "formal_outer_canary_supervisor.py", "formal_outer_canary_wire.py")
UPGRADE_MANIFEST = "upgrade.json"
TEAM_BUTTON = b'id="outer-team-canary-submit"'
TEAM_ASSETS = ("goal_team.js", "goal_team.css", "task_presentation.js")


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
                    require(tasks_idle(db, ACTIVE), "有活动的正式工单，保留原工作台继续处理")
            elif name == "afr-topics.sqlite3" and idle:
                running, = db.execute("SELECT COUNT(*) FROM afr_topics_v1 WHERE status='running'").fetchone()
                require(running == 0, "有运行中的 AFR 任务，保留原工作台继续处理")
            if backup is not None:
                backup_db(db, backup / name)
    return task_ids


def tasks_idle(db, statuses):
    marks = ",".join("?" for _ in statuses)
    active = db.execute(f"""SELECT 1 FROM tasks WHERE json_extract(body,'$.status') IN ({marks})
        OR json_type(body,'$.active_call') != 'null' LIMIT 1""", statuses).fetchone()
    return active is None


def backup_db(db, target):
    with closing(sqlite3.connect(target)) as replica:
        db.backup(replica)
        require(replica.execute("PRAGMA quick_check").fetchone() == ("ok",), "SQLite 备份完整性检查失败")
    with target.open("rb") as saved:
        os.fsync(saved.fileno())
    sync_directory(target.parent)


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


@contextmanager
def deployment_lock():
    lock = STATE / ".outer-canary-deployment.lock"
    with os.fdopen(os.open(lock, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600), "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("另一个正式灰度操作正在进行") from None
        yield


def read_canary(*, team=None):
    fetch, get, known = read_legacy()
    home = fetch("/")
    workbench = fetch("/workbench")
    drafts = get("/api/outer-canary/drafts")
    require(b'id="outer-canary-submit"' in home and b'/outer-canary/app.js' in workbench,
            "正式页面的合成索引入口不可用")
    require(drafts["enabled"] is True and isinstance(drafts["drafts"], list), "灰度草案账本不可用")
    team_visible = TEAM_BUTTON in home
    require((drafts.get("goal_team_enabled") is True) == team_visible, "正式团队入口与侧服务功能开关不一致")
    if team is not None:
        require(team_visible == team, "正式团队灰度入口未达到目标状态")
    if team_visible:
        for name in TEAM_ASSETS:
            require(fetch(f"/outer-canary/{name}"), f"团队灰度资源不可用：{name}")
    return known, team_visible


def canary_config(originals=None):
    for path in (CANARY, CANARY / "runtime", CANARY / "runtime/hub", CANARY / "runtime/workbench"):
        require(path.is_dir() and not path.is_symlink(), "现役灰度目录不可用或为链接")
    profile = CANARY / "profile.json"
    for path in (BOARD_PLIST, SIDE_PLIST, profile, CANARY / "gateway.key"):
        require(path.is_file() and not path.is_symlink(), "现役灰度配置不可用或为链接")
    inputs = tuple(path.read_bytes() for path in (BOARD_PLIST, SIDE_PLIST, profile))
    board, side = map(plistlib.loads, inputs[:2])
    args = board["ProgramArguments"]
    switch = ["--outer-canary-profile", str(profile)]
    require(args[-2:] == switch, "正式工作台未使用对应的现役灰度配置")
    board["ProgramArguments"] = args[:-2]
    python = select_python(board)
    if originals is not None:
        require(board == plistlib.loads(originals[PLIST_BACKUP][0]), "正式 LaunchAgent 与原始归档不匹配")
    side_args = side["ProgramArguments"]
    expected = [python, "-B", str(CANARY / SIDE_FILES[1]), "--root", str(CANARY)]
    require(side.get("Label") == SIDE and side.get("Program", python) == python
            and side.get("WorkingDirectory") == str(CANARY) and len(side_args) == 9
            and side_args[:5] == expected and side_args[5:8:2] == ["--board-port", "--hub-port"],
            "现役侧 LaunchAgent 未指向对应灰度进程")
    ports = tuple(int(port) for port in side_args[6:9:2])
    require(len(set(ports)) == 2 and all(1024 <= port <= 65535 and port != 8765 for port in ports)
            and json.loads(inputs[2])["board_port"] == ports[0], "灰度 LaunchAgent 端口与前端配置不一致")
    return ports, tuple(hashlib.sha256(data).hexdigest() for data in inputs)


def read_side_state(*, backup=None, idle=False, live=True):
    runtime = CANARY / "runtime"
    records = {}
    paths = ("workbench/tasks.sqlite3", "hub/hub.db", *(f"workbench/{name}" for name in DATABASES[1:]))
    for relative in paths:
        path = runtime / relative
        if relative in paths[2:] and not path.exists() and not path.is_symlink():
            continue
        require(path.is_file() and not path.is_symlink(), "侧灰度数据库不可用或为链接")
        with sqlite_source(path, live=live) as db:
            require(db.execute("PRAGMA quick_check").fetchone() == ("ok",), "侧灰度数据库完整性检查失败")
            if relative == paths[0]:
                if idle:
                    require(tasks_idle(db, (*ACTIVE, "created")), "侧灰度有待执行或正在执行的工单")
                    require(not db.execute("SELECT 1 FROM outer_events WHERE state IN ('PENDING','RUNNING') LIMIT 1").fetchone(),
                            "侧灰度有待处理的接单事件")
                for table in ("tasks", "outer_tickets", "outer_events"):
                    records[table] = {row[0] for row in db.execute(f"SELECT id FROM {table}")}
            elif relative == paths[1]:
                if idle:
                    require(not db.execute("SELECT 1 FROM runs WHERE state IN ('queued','claimed','running','unknown') LIMIT 1").fetchone(),
                            "侧 Hub 有待执行或状态未确认的派工")
                records["runs"] = {row[0] for row in db.execute("SELECT id FROM runs")}
            if backup is not None:
                destination = backup / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                backup_db(db, destination)
    return records


def side_offline(ports):
    if launch("print", SIDE, checked=False):
        return False
    for port in ports:
        with socket.socket() as listener:
            listener.settimeout(.3)
            if listener.connect_ex(("127.0.0.1", port)) != errno.ECONNREFUSED:
                return False
    lock = CANARY / "supervisor.lock"
    if lock.exists() or lock.is_symlink():
        with os.fdopen(os.open(lock, os.O_RDONLY | os.O_NOFOLLOW), "r") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
    return True


def stop_canary(ports):
    stop_front()
    if launch("print", SIDE, checked=False):
        requested = launch("bootout", SIDE, checked=False)
        require(requested or not launch("print", SIDE, checked=False), "侧灰度 LaunchAgent 未能停止")
    wait_check(lambda: side_offline(ports), "侧灰度子服务未停稳")


def start_canary():
    try:
        if not launch("print", SIDE, checked=False):
            log = CANARY / "supervisor.log"
            offset = log.stat().st_size if log.exists() else 0
            launch("bootstrap", SIDE_PLIST)

            def fresh_ready():
                with log.open("rb") as source:
                    source.seek(offset)
                    return b"READY" in source.read().splitlines()

            wait_check(fresh_ready, "侧灰度服务未就绪", 80)
    finally:
        # 侧服务就绪失败时也让正式旧入口先恢复。
        start_front()


def fingerprint(path):
    if not path.exists() and not path.is_symlink():
        return None
    entries = []
    for item in [path, *sorted(path.rglob("*"))]:
        mode = item.lstat().st_mode
        require(stat.S_ISDIR(mode) or stat.S_ISREG(mode), "灰度代码目录含链接或特殊文件")
        entries.append((str(item.relative_to(path)), mode, sha(item) if stat.S_ISREG(mode) else None))
    return hashlib.sha256(json.dumps(entries).encode()).hexdigest()


CODE_LAYOUT = {"front": (*FRONT_FILES, *ADDITIONS), "side": SIDE_FILES}


def code_state(roots):
    return {group: {name: fingerprint(roots[group] / name) for name in names} for group, names in CODE_LAYOUT.items()}


def code_snapshot(source, target):
    if source.is_dir():
        shutil.copytree(source, target)
        for path in sorted(target.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if path.is_dir():
                sync_directory(path)
            else:
                with path.open("rb") as saved:
                    os.fsync(saved.fileno())
        sync_directory(target)
    else:
        atomic(target, source.read_bytes(), stat.S_IMODE(source.stat().st_mode))
    sync_directory(target.parent)


def replace_code(source_roots, destination_roots):
    for group, names in CODE_LAYOUT.items():
        for name in names:
            source, destination = source_roots[group] / name, destination_roots[group] / name
            if source.is_file():
                atomic(destination, source.read_bytes(), stat.S_IMODE(source.stat().st_mode))
            else:
                with tempfile.TemporaryDirectory(prefix=".outer-canary-switch-", dir=destination.parent) as temporary:
                    staged = Path(temporary) / "new"
                    code_snapshot(source, staged)
                    if destination.exists():
                        destination.rename(Path(temporary) / "old")
                    staged.rename(destination)
                    sync_directory(destination.parent)


def require_candidate(target):
    repo = Path(__file__).resolve().parents[1]
    require(re.fullmatch(r"[0-9a-f]{40}", target or ""), "请提供已审查目标的完整 Git SHA")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    status = subprocess.check_output(["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=repo)
    require(head == target and not status, "当前 checkout 须干净且位于指定的已提交目标 SHA")
    return repo


def build_upgrade(repo, target, original, staging):
    archive = staging / "source.tar"
    source = staging / "source"
    source.mkdir()
    subprocess.run(["git", "archive", "--format=tar", f"--output={archive}", target], cwd=repo, check=True)
    with tarfile.open(archive) as packed:
        members = packed.getmembers()
        require(all((entry.isfile() or entry.isdir()) and not Path(entry.name).is_absolute()
                    and ".." not in Path(entry.name).parts for entry in members), "目标提交归档含链接或无效路径")
        packed.extractall(source, members=members)
    bundle = staging / "bundle"
    # 渲染进程从目标提交读取源码，避免构建期间混入本地文件。
    python = select_python(plistlib.loads(read_recovery(original)[PLIST_BACKUP][0]))
    subprocess.run([python, "-E", "-s", "-B", str(source / "scripts/prepare_formal_canary.py"), str(original), str(bundle)],
                   check=True)
    for name in SIDE_FILES[1:]:
        shutil.copy2(source / "scripts" / name, bundle / name)
    require(all((bundle / "front/outer-canary" / name).is_file() for name in TEAM_ASSETS)
            and TEAM_BUTTON in (bundle / "front/index.html").read_bytes(), "构建包缺少固定合成团队入口")
    return {"front": bundle / "front", "side": bundle}


def write_upgrade_manifest(backup, manifest):
    atomic(backup / UPGRADE_MANIFEST, (json.dumps(manifest, indent=2) + "\n").encode(), 0o600)


def assert_preserved(known, formal, side, *, team):
    observed, _ = read_canary(team=team)
    side_now = read_side_state()
    require(known <= observed and formal <= read_state(), "切换后存在无法回读的原正式工单")
    require(all(ids <= side_now[name] for name, ids in side.items()), "切换后存在无法回读的侧灰度记录")


def upgrade(original, target):
    repo = require_candidate(target)
    originals = read_recovery(original)
    ports, config = canary_config(originals)
    require(launch("print", BOARD) and launch("print", SIDE), "正式与侧灰度 LaunchAgent 须处于运行状态")
    known, prior_team = read_canary()
    live_formal, live_side = read_state(idle=True), read_side_state(idle=True)
    live_roots = {"front": APP, "side": CANARY}
    recovery_dir = original.parent
    with tempfile.TemporaryDirectory(prefix="autoloop-outer-upgrade-") as temporary:
        candidate = build_upgrade(repo, target, original, Path(temporary))
        require_candidate(target)
        require(read_recovery(original) == originals, "构建期间原始归档发生变动")
        before, after = code_state(live_roots), code_state(candidate)
        require(all(all(files.values()) for state in (before, after) for files in state.values()), "灰度部署代码不完整")
        require(before != after, "指定目标的灰度代码已部署")
        backup = Path(tempfile.mkdtemp(prefix="outer-canary-upgrade-", dir=recovery_dir))
        sync_directory(recovery_dir)
        previous = {group: backup / group for group in CODE_LAYOUT}
        for group, names in CODE_LAYOUT.items():
            previous[group].mkdir()
            for name in names:
                code_snapshot(live_roots[group] / name, previous[group] / name)
        require(code_state(previous) == before, "现役代码备份与切换前摘要不一致")
        manifest = {"version": 1, "port": 8765, "candidate": target, "prior_team": prior_team,
                    "config_sha256": config, "before": before, "after": after}
        write_upgrade_manifest(backup, manifest)
        print(f"正式灰度升级恢复点 {backup}", flush=True)
        changed = False
        read_state(idle=True)
        read_side_state(idle=True)
        try:
            stop_canary(ports)
            formal_backup = backup / "formal-sqlite"
            formal_backup.mkdir()
            formal = read_state(backup=formal_backup, idle=True, live=False)
            side = read_side_state(backup=backup / "side-sqlite", idle=True, live=False)
            sync_directory(backup / "side-sqlite")
            sync_directory(backup)
            require(live_formal <= formal and all(ids <= side[name] for name, ids in live_side.items()),
                    "停机后发现记录缺失，取消升级")
            require(canary_config(originals) == (ports, config) and code_state(live_roots) == before,
                    "升级前现役配置或代码发生变动")
            changed = True
            replace_code(candidate, live_roots)
            require(code_state(live_roots) == after, "升级文件与目标构建摘要不一致")
            start_canary()
            assert_preserved(known, formal, side, team=True)
            require(canary_config(originals) == (ports, config), "切换后现役灰度配置发生变动")
        except BaseException:
            try:
                if changed:
                    stop_canary(ports)
                    require(code_state(previous) == before, "升级前代码备份摘要发生变动")
                    replace_code(previous, live_roots)
                    require(code_state(live_roots) == before, "自动回退后代码摘要不一致")
            finally:
                start_canary()
            if changed:
                assert_preserved(known, formal, side, team=prior_team)
            print("升级失败，已恢复升级前服务。", flush=True)
            raise
        print(f"正式灰度已升级至 {target}，原工单与侧灰度记录可读。", flush=True)


def rollback_upgrade(backup):
    expected = STATE / "deploy-before"
    require(backup.parent.resolve() == expected.resolve() and backup.name.startswith("outer-canary-upgrade-")
            and backup.is_dir() and not backup.is_symlink(), "指定的灰度升级恢复点无效")
    marker = backup / UPGRADE_MANIFEST
    require(marker.is_file() and not marker.is_symlink(), "缺少有效的灰度升级恢复清单")
    manifest = json.loads(marker.read_bytes())
    require(manifest.get("version") == 1 and manifest.get("port") == 8765
            and type(manifest.get("prior_team")) is bool, "灰度升级恢复清单无效")
    previous = {group: backup / group for group in CODE_LAYOUT}
    live_roots = {"front": APP, "side": CANARY}
    before, after = manifest["before"], manifest["after"]
    require(all(path.is_dir() and not path.is_symlink() for path in previous.values())
            and code_state(previous) == before and all(all(files.values()) for files in before.values()),
            "灰度升级恢复点代码摘要不一致")
    ports, config = canary_config()
    require(list(config) == manifest["config_sha256"], "现役灰度配置与恢复点不一致")
    current = code_state(live_roots)
    require(all(current[group][name] in (before[group][name], after[group][name], None)
                for group, names in CODE_LAYOUT.items() for name in names), "现役代码包含此恢复点以外的版本")
    front_up = launch("print", BOARD, checked=False)
    side_up = launch("print", SIDE, checked=False)
    known = read_legacy()[2] if front_up else set()
    read_state(idle=True, live=front_up)
    read_side_state(idle=True, live=side_up)
    try:
        stop_canary(ports)
        formal = read_state(idle=True, live=False)
        side = read_side_state(idle=True, live=False)
        require(canary_config() == (ports, config) and code_state(live_roots) == current,
                "停机后灰度配置或代码变化，取消回退")
        replace_code(previous, live_roots)
        require(code_state(live_roots) == before, "回退后代码摘要不一致")
    finally:
        start_canary()
    assert_preserved(known, formal, side, team=manifest["prior_team"])
    print("正式灰度已回退至本次升级前，现役数据库保留在原位置。", flush=True)


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--preflight", action="store_true")
    operation.add_argument("--install", action="store_true")
    operation.add_argument("--rollback", type=Path)
    operation.add_argument("--upgrade", type=Path, metavar="原始恢复点")
    operation.add_argument("--rollback-upgrade", type=Path, metavar="升级恢复点")
    parser.add_argument("--target-sha", help="--upgrade 对应的完整 Git SHA")
    args = parser.parse_args()
    if bool(args.upgrade) != bool(args.target_sha):
        parser.error("--upgrade 和 --target-sha 须同时指定")
    if args.preflight:
        _, _, known = read_legacy()
        ids = read_state(idle=True)
        print(f"正式旧单、现役 AFR 和数据库检查通过。账本 {len(ids)} 张，前台 {len(known)} 张。")
    else:
        with deployment_lock():
            if args.install:
                install()
            elif args.upgrade:
                upgrade(args.upgrade.absolute(), args.target_sha)
            elif args.rollback_upgrade:
                rollback_upgrade(args.rollback_upgrade.absolute())
            else:
                rollback(args.rollback.absolute())
