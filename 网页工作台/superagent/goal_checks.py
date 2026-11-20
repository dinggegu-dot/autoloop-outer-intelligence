"""Version 1 executable goal contracts and offline verification."""
import errno
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import stat
from tempfile import TemporaryDirectory
import unicodedata


MAX_STAGES = 6
MAX_CHECKS = 4
MAX_ARGS = 16
MAX_FIXTURES = 8
MAX_TEXT_BYTES = 16 * 1024
MAX_CONTRACT_BYTES = 256 * 1024
MAX_WORKSPACE_BYTES = 8 * 1024 * 1024
MAX_WORKSPACE_FILE = 1024 * 1024
MAX_ENTRIES = 128
MAX_PATH_DEPTH = 8
_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}")
_COMPONENT = re.compile(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,79}")
_SECRET = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bBearer\s+[A-Za-z0-9._~+/-]+=*|"
    r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{16,}|(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{16,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|(?:AKIA|ASIA)[A-Z0-9]{16})\b|"
    r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@|"
    r"\b(?:api[-_ ]?key|access[-_ ]?token|password|secret)\s*[:=]\s*\S+",
    re.IGNORECASE,
)
_SECRET_KEYS = {"apikey", "accesskey", "accesstoken", "authtoken", "authorization",
                "credential", "credentials", "password", "privatekey", "secret", "token"}


def _identifier(value):
    if type(value) is not str or not _ID.fullmatch(value):
        raise ValueError("ID must have 1–64 ASCII letters, digits, underscores or hyphens")
    return value


def _fields(value, fields):
    if type(value) is not dict or value.keys() != set(fields):
        raise ValueError("unexpected or missing contract fields")


def _text(value, limit):
    if type(value) is not str or len(value) > limit:
        raise ValueError("contract text exceeds its limit or is not a string")
    if "\0" in value or len(value.encode("utf-8")) > limit or _SECRET.search(value):
        raise ValueError("contract text is invalid, too large or looks like a credential")
    return value


def _path(value):
    value = _text(value, 240)
    parts = value.split("/")
    if len(parts) > MAX_PATH_DEPTH or any(not _COMPONENT.fullmatch(part) for part in parts):
        raise ValueError("paths must be relative, plain ASCII paths without dot segments")
    return value


def _fold(path):
    # macOS commonly resolves case and Unicode aliases to the same file.
    return unicodedata.normalize("NFD", path).casefold()


def _overlap(left, right):
    left, right = _fold(left), _fold(right)
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def _argument(value):
    value = _text(value, 512)
    if any(ch in value for ch in ("\\", "~", "\r", "\n")):
        raise ValueError("arguments must be literal, single-line values")
    # Paths are relative to the staged workspace. Support both positional and --flag=path.
    payload = value.split("=", 1)[1] if value.startswith("-") and "=" in value else value
    if ":" in payload or payload in (".", ".."):
        raise ValueError("external or dot-path arguments are not supported")
    if "/" in payload:
        _path(payload)
    if re.sub(r"[^a-z]", "", value.partition("=")[0].lower()) in _SECRET_KEYS:
        raise ValueError("credential arguments are not supported")
    return value


def _json(value, remaining, depth=0):
    remaining[0] -= 1
    if remaining[0] < 0 or depth > 12:
        raise ValueError("expected JSON exceeds its depth or node limit")
    kind = type(value)
    if value is None or kind is bool:
        return value
    if kind is int and value.bit_length() <= 256:
        return value
    if kind is float and math.isfinite(value):
        return value
    if kind is str:
        return _json_text(value, MAX_TEXT_BYTES, remaining)
    if kind is list and len(value) <= remaining[0]:
        return [_json(item, remaining, depth + 1) for item in value]
    if kind is dict and len(value) <= remaining[0]:
        result = {}
        for key, item in value.items():
            key = _json_text(key, 256, remaining)
            if item not in (None, "") and re.sub(r"[^a-z]", "", key.lower()) in _SECRET_KEYS:
                raise ValueError("expected JSON contains a credential field")
            result[key] = _json(item, remaining, depth + 1)
        return dict(sorted(result.items()))
    raise ValueError("expected value must be bounded, finite JSON")


def _json_text(value, limit, remaining):
    value = _text(value, limit)
    remaining[1] -= len(value.encode("utf-8"))
    if remaining[1] < 0:
        raise ValueError("expected JSON text exceeds 64 KiB")
    return value


def _encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _normalize_stage(stage, known_checks):
    _fields(stage, ("id", "checks"))
    stage_id = _identifier(stage["id"])
    if type(stage["checks"]) is not list or not 1 <= len(stage["checks"]) <= MAX_CHECKS:
        raise ValueError("each stage must have 1–4 checks")
    checks = []
    for check in stage["checks"]:
        _fields(check, ("id", "script", "args", "files", "expected"))
        check_id = _identifier(check["id"])
        if check_id in known_checks:
            raise ValueError("check IDs must be globally unique")
        known_checks.add(check_id)
        script = _path(check["script"])
        if not script.endswith(".py"):
            raise ValueError("check script must be a relative .py path")
        args = check["args"]
        if type(args) is not list or len(args) > MAX_ARGS:
            raise ValueError("too many arguments or args is not a list")
        inputs = check["files"]
        if type(inputs) is not dict or len(inputs) > MAX_FIXTURES:
            raise ValueError("too many fixtures or files is not an object")
        files = {}
        for name, content in inputs.items():
            name = _path(name)
            if any(_overlap(name, prior) for prior in files):
                raise ValueError("fixture paths overlap")
            files[name] = _text(content, MAX_TEXT_BYTES)
        expected = _json(check["expected"], [2048, 64 * 1024])
        if len(_encode(expected).encode("utf-8")) > 64 * 1024:
            raise ValueError("expected JSON exceeds 64 KiB")
        checks.append({"id": check_id, "script": script, "args": [_argument(arg) for arg in args],
                       "files": dict(sorted(files.items())), "expected": expected})
    return {"id": stage_id, "checks": checks}


def normalize(contract: dict) -> dict:
    """Return a detached v1 contract; invalid contracts raise ValueError."""
    _fields(contract, ("version", "stages"))
    if type(contract["version"]) is not int or contract["version"] != 1:
        raise ValueError("unsupported contract version")
    stages = contract["stages"]
    if type(stages) is not list or not 1 <= len(stages) <= MAX_STAGES:
        raise ValueError("contract must have 1–6 stages")
    known_checks = set()
    normalized = [_normalize_stage(stage, known_checks) for stage in stages]
    if len({stage["id"] for stage in normalized}) != len(normalized):
        raise ValueError("stage IDs must be unique")
    scripts = {check["script"] for stage in normalized for check in stage["checks"]}
    for stage in normalized:
        for check in stage["checks"]:
            if any(_overlap(name, script) for name in check["files"] for script in scripts):
                raise ValueError("fixture path overlaps a contract script")
    result = {"version": 1, "stages": normalized}
    if len(_encode(result).encode("utf-8")) > MAX_CONTRACT_BYTES:
        raise ValueError("contract exceeds 256 KiB")
    return result


def _open_root(workspace):
    # Anchor every lookup at a descriptor. Refuse symlinks in ancestor paths as well.
    path = Path(workspace).absolute()
    if ".." in path.parts:
        raise ValueError("workspace path contains a parent segment")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            # /tmp and /var are system aliases on macOS. Workbench supplies a physical path.
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _snapshot(workspace):
    files, occupied, aliases = {}, {}, set()
    total = 0

    def visit(fd, prefix, depth):
        nonlocal total
        if depth > MAX_PATH_DEPTH:
            raise ValueError("workspace directory depth exceeds its limit")
        with os.scandir(fd) as entries:
            for entry in entries:
                relative = prefix + entry.name
                alias = _fold(relative)
                if alias in aliases or len(occupied) >= MAX_ENTRIES:
                    raise ValueError("workspace has path aliases or too many entries")
                aliases.add(alias)
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise ValueError("workspace contains a symbolic link")
                directory = stat.S_ISDIR(info.st_mode)
                occupied[relative] = directory
                if directory:
                    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
                    nested = os.open(entry.name, flags, dir_fd=fd)
                    try:
                        visit(nested, relative + "/", depth + 1)
                    finally:
                        os.close(nested)
                elif (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                      and info.st_size <= MAX_WORKSPACE_FILE):
                    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                    with os.fdopen(os.open(entry.name, flags, dir_fd=fd), "rb") as handle:
                        current = os.fstat(handle.fileno())
                        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                            raise ValueError("workspace changed while staging")
                        if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
                            raise ValueError("workspace changed while staging")
                        data = handle.read(MAX_WORKSPACE_FILE + 1)
                        final = os.fstat(handle.fileno())
                    if (final.st_size, final.st_mtime_ns, final.st_ctime_ns) != (
                        current.st_size, current.st_mtime_ns, current.st_ctime_ns
                    ):
                        raise ValueError("workspace changed while staging")
                    if len(data) > MAX_WORKSPACE_FILE or total + len(data) > MAX_WORKSPACE_BYTES:
                        raise ValueError("workspace snapshot exceeds its size limit")
                    files[relative] = data
                    total += len(data)

    root = _open_root(workspace)
    try:
        visit(root, "", 0)
    finally:
        os.close(root)
    return files, occupied


def _policy():
    path = Path(__file__).resolve().parents[1] / "验收样例" / "目标驱动CSV" / "verifier.py"
    spec = importlib.util.spec_from_file_location("_goal_checks_seatbelt", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("trusted CSV Seatbelt policy unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_BOOTSTRAP = """
import runpy
import sys
from pathlib import Path
script = sys.argv[1]
sys.argv = sys.argv[1:]
sys.path[:0] = [str(Path(script).parent), str(Path.cwd())]
runpy.run_path(script, run_name="__main__")
"""


def _strict_output(output):
    def pairs(items):
        result = dict(items)
        if len(result) != len(items):
            raise ValueError("duplicate JSON key")
        return result

    def invalid(_):
        raise ValueError("nonfinite JSON number")

    return _encode(json.loads(output.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid))


def _fixture_conflicts(name, occupied):
    for path, directory in occupied.items():
        if not _overlap(name, path):
            continue
        # Adding a fixture inside an existing directory is safe if the directory spelling matches.
        if directory and name.startswith(path + "/"):
            continue
        return True
    return False


def _verify_check(root, policy, check, files, occupied):
    if check["script"] not in files:
        raise ValueError("check script is missing or is not a bounded plain workspace file")
    if any(_fixture_conflicts(name, occupied) for name in check["files"]):
        raise ValueError("synthetic fixture overlaps a workspace artifact")
    stage = root / "workspace"
    stage.mkdir(mode=0o700)
    try:
        for relative, directory in sorted(occupied.items()):
            if directory:
                (stage / relative).mkdir(mode=0o700)
        for relative, data in sorted(files.items()):
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with target.open("xb") as handle:
                handle.write(data)
        for relative, content in check["files"].items():
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with target.open("xb") as handle:
                handle.write(content.encode("utf-8"))

        python, profile = policy._seatbelt(stage)
        profile += "\n(allow file-read-data (subpath " + json.dumps(str(stage)) + "))\n"
        sentinel = root / "host-sentinel"
        sentinel.write_bytes(b"not in the workspace")
        try:
            probe = policy._run(stage, python, profile, [
                "-c", policy.PROBE, str(sentinel), str(stage / ".write-probe")
            ])
        except (OSError, RuntimeError, ValueError) as exc:
            # This is trusted probe output, not candidate-controlled stderr.
            raise ValueError(f"Seatbelt unavailable or probe failed: {str(exc)[:300]}") from None
        if probe.strip() != b"seatbelt-ok":
            raise ValueError("Seatbelt probe did not establish isolation")

        argv = ["-c", _BOOTSTRAP, check["script"], *check["args"]]
        for attempt in (1, 2):
            try:
                output = policy._run(stage, python, profile, argv)
            except TimeoutError:
                raise ValueError(f"execution {attempt} exceeded the 3-second limit") from None
            except (OSError, RuntimeError, ValueError):
                raise ValueError(f"execution {attempt} failed in Seatbelt or exceeded its output limit") from None
            try:
                actual = _strict_output(output)
            except (ValueError, OverflowError, RecursionError):
                raise ValueError(f"execution {attempt} did not emit one valid UTF-8 JSON value") from None
            if actual != _encode(check["expected"]):
                raise ValueError(f"execution {attempt} did not match expected JSON")
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            raise ValueError("staging path overlaps another file") from None
        raise


def verify(workspace: Path, stage: dict) -> dict:
    """Verify one v1 stage using the same immutable snapshot for all its checks."""
    try:
        stage = normalize({"version": 1, "stages": [stage]})["stages"][0]
    except ValueError as exc:
        return {"passed": False, "checks": [{"id": "stage", "passed": False, "detail": str(exc)}]}
    checks = []
    try:
        files, occupied = _snapshot(workspace)
        policy = _policy()
    except (OSError, RuntimeError, ValueError) as exc:
        detail = "could not safely snapshot workspace or load Seatbelt policy" if isinstance(exc, OSError) else str(exc)
        return {"passed": False, "checks": [
            {"id": check["id"], "passed": False, "detail": detail} for check in stage["checks"]
        ]}
    for check in stage["checks"]:
        try:
            with TemporaryDirectory(prefix="goal-check-", dir="/private/tmp") as directory:
                _verify_check(Path(directory).resolve(), policy, check, files, occupied)
            row = {"id": check["id"], "passed": True, "detail": "expected JSON matched in both isolated executions"}
        except (OSError, RuntimeError, ValueError) as exc:
            detail = "could not create or read isolated staging" if isinstance(exc, OSError) else str(exc)
            row = {"id": check["id"], "passed": False, "detail": detail}
        checks.append(row)
    return {"passed": all(check["passed"] for check in checks), "checks": checks}
