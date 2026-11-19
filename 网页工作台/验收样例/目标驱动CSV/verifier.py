"""在 macOS Seatbelt 中验收 workspace/csv_check.py。"""
import json
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic

FIXTURE = Path(__file__).with_name("input.csv")
ORIGINAL = "id,category\nA,书籍\nB,\nA,工具\nC,\n".encode("utf-8")
TIMEOUT = 3.0
OUTPUT_LIMIT = 64 * 1024
FILE_LIMIT = 1024 * 1024

PROBE = """
import errno
import os
import socket
import sys

def denied(action):
    try:
        action()
    except OSError as exc:
        if exc.errno in (errno.EPERM, errno.EACCES):
            return
        raise
    raise RuntimeError("Seatbelt did not deny an operation")

denied(lambda: os.open(sys.argv[1], os.O_RDONLY))
denied(lambda: os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT))
def bind():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
denied(bind)
def connect():
    with socket.socket() as sock:
        sock.connect(("127.0.0.1", 1))
denied(connect)
print("seatbelt-ok")
"""


def _regular_bytes(path: Path) -> bytes:
    # 拒绝链接、FIFO 和过大文件，避免验收器在沙箱外跟随链接或被阻塞。
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError(f"{path.name} 必须是普通文件")
        data = handle.read(FILE_LIMIT + 1)
    if len(data) > FILE_LIMIT:
        raise ValueError(f"{path.name} 超过验收文件大小上限")
    return data


def _seatbelt(stage: Path) -> tuple[str, str]:
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise RuntimeError("验收需要 macOS /usr/bin/sandbox-exec")
    python = Path(sys.executable).resolve(strict=True)
    prefix = Path(sys.base_prefix).resolve(strict=True)
    framework_binary = prefix / "Resources/Python.app/Contents/MacOS/Python"
    if framework_binary.is_file():
        python = framework_binary.resolve(strict=True)
    if not any(parent in python.parents for parent in (prefix, Path("/usr/bin"))):
        raise RuntimeError("无法确定 Python 解释器的只读依赖目录")
    # 只放行系统及解释器运行时、两份指定输入和已继承的标准 IO。
    # 不授予任何文件写入、网络、进程派生或其他可执行文件权限。
    read_dirs = ("/System", "/usr/lib", "/usr/share/zoneinfo", str(prefix / "lib"))
    if python == framework_binary:
        read_dirs += (str(prefix),)
    read_paths = " ".join(f"(subpath {json.dumps(path, ensure_ascii=False)})" for path in read_dirs)
    read_files = " ".join(
        f"(literal {json.dumps(str(path), ensure_ascii=False)})"
        for path in ("/", "/dev/urandom", python, stage / "csv_check.py", stage / "input.csv")
    )
    executable = json.dumps(str(python), ensure_ascii=False)
    profile = f"""(version 1)
(deny default)
(allow process-exec (literal {executable}))
(allow file-read-metadata)
(allow file-read-data {read_paths} {read_files})
(allow file-map-executable {read_paths} (literal {executable}))
(allow sysctl-read)
(deny network*)
"""
    return str(python), profile


def _run(stage: Path, python: str, profile: str, args: list[str]) -> bytes:
    argv = ["/usr/bin/sandbox-exec", "-p", profile, python, "-I", "-S", "-B", *args]
    with subprocess.Popen(
        argv, cwd=stage, env={"PATH": "/usr/bin:/bin", "HOME": str(stage), "LANG": "en_US.UTF-8"},
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    ) as child, selectors.DefaultSelector() as selector:
        output = {"stdout": bytearray(), "stderr": bytearray()}
        selector.register(child.stdout, selectors.EVENT_READ, "stdout")
        selector.register(child.stderr, selectors.EVENT_READ, "stderr")
        deadline = monotonic() + TIMEOUT
        try:
            while selector.get_map():
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"超过 {TIMEOUT:g} 秒")
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output[key.data].extend(chunk)
                    if len(output[key.data]) > OUTPUT_LIMIT:
                        raise ValueError("进程输出超过 64 KiB")
            child.wait(timeout=max(0, deadline - monotonic()))
        except (TimeoutError, subprocess.TimeoutExpired):
            raise TimeoutError(f"超过 {TIMEOUT:g} 秒") from None
        finally:
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
    if child.returncode:
        stderr = output["stderr"].decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"沙箱命令退出 {child.returncode}：{stderr[:500]}")
    return bytes(output["stdout"])


def verify(workspace: Path) -> dict:
    checks = []

    def record(check_id: str, passed: bool, detail: str) -> None:
        checks.append({"id": check_id, "passed": passed, "detail": detail})

    def result() -> dict:
        return {"passed": all(check["passed"] for check in checks), "checks": checks}

    try:
        workspace = workspace.resolve(strict=True)
        candidate = _regular_bytes(workspace / "csv_check.py")
        if _regular_bytes(FIXTURE) != ORIGINAL:
            raise ValueError("验收原件 input.csv 与约定字节不一致")
        original_path = workspace / "input.csv"
        if original_path.exists() or original_path.is_symlink():
            if _regular_bytes(original_path) != ORIGINAL:
                raise ValueError("workspace/input.csv 与验收原件不一致")
        else:
            original_path = None
        record("inputs", True, "找到待验脚本，验收原件内容正确")
    except (OSError, ValueError) as exc:
        record("inputs", False, str(exc))
        return result()

    cases = (
        ("ordinary", ORIGINAL, {"empty_category_rows": [2, 4], "duplicate_id_rows": [3]}),
        ("counterexample", b"id,category\nX,\nX,one\nX,\nY,two\n",
         {"empty_category_rows": [1, 3], "duplicate_id_rows": [2, 3]}),
        ("header_only", b"id,category\n", {"empty_category_rows": [], "duplicate_id_rows": []}),
    )
    try:
        with TemporaryDirectory(prefix="goal-csv-verify-", dir="/private/tmp") as directory:
            root = Path(directory).resolve()
            stage = root / "stage"
            stage.mkdir(mode=0o700)
            sentinel = root / "host-sentinel"
            sentinel.write_bytes(b"unreadable to candidate")
            python, profile = _seatbelt(stage)
            probe = _run(stage, python, profile, ["-c", PROBE, str(sentinel), str(stage / "write-probe")])
            if probe.strip() != b"seatbelt-ok":
                raise RuntimeError("沙箱探针未确认读取、写入和网络限制")
            record("sandbox", True, "Seatbelt 拒绝读取未授权文件、写入、绑定端口和发起连接")

            script = stage / "csv_check.py"
            script.write_bytes(candidate)
            input_path = stage / "input.csv"
            for check_id, data, expected in cases:
                input_path.write_bytes(data)
                try:
                    outputs = [_run(stage, python, profile, [str(script), str(input_path)]) for _ in range(2)]
                    actual = json.loads(outputs[0])
                    # bool 是 int 的子类，按 JSON 类型比较避免 true 冒充索引 1。
                    correct = json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)
                    stable = outputs[0] == outputs[1]
                    intact = _regular_bytes(input_path) == data
                    record(check_id, correct and stable and intact,
                           f"输出匹配={correct}，重复输出一致={stable}，输入保留={intact}")
                except (OSError, RuntimeError, ValueError) as exc:
                    record(check_id, False, str(exc))
    except (OSError, RuntimeError, ValueError) as exc:
        record("sandbox", False, str(exc))

    try:
        intact = _regular_bytes(FIXTURE) == ORIGINAL and (
            original_path is None or _regular_bytes(original_path) == ORIGINAL
        )
        record("original", intact, "验收前后比较原件字节")
    except (OSError, ValueError) as exc:
        record("original", False, str(exc))
    return result()
