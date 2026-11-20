"""本机 AFR 扫描与版本化派工。只将哈希、尝试次数和计数传给 Hub。"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

DIGEST = re.compile(r"[a-f0-9]{64}")
RETRY_DELAYS = (60, 300, 0)
RECOVERY_SECONDS = 60
SETTLE_SECONDS = 4
POLL_SECONDS = 1
TRANSIENT = {"in_progress", "codex_timeout", "codex_failed", "io_error", "subprocess_timeout"}
STOPPED = {"requires_ocr", "codex_unavailable", "codex_authentication", "local_only", "invalid_request",
           "evidence_changed", "user_edit_conflict", "version_conflict", "summary_requires_review",
           "invalid_coverage", "invalid_plan", "invalid_path", "invalid_vault", "invalid_manifest",
           "encrypted_pdf", "oversized_unit", "unsupported_file", "not_found"}
SCAN_CODES = TRANSIENT | STOPPED | {"file_in_progress"}


def write_json(path, body):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        tmp = Path(stream.name)
        try:
            json.dump(body, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)


def read_json(path):
    return json.loads(path.read_text())


def config(path):
    value = read_json(path)
    base = Path(value["state"])
    if not base.is_dir() or base.stat().st_uid != os.getuid() or base.stat().st_mode & 0o077:
        raise ValueError("invalid private state directory")
    for name in ("inbox", "vault", "cwd", "python", "approvals"):
        location = Path(value[name])
        if not location.is_absolute() or (name != "python" and location.is_symlink()):
            raise ValueError("expected fixed absolute paths")
        value[name] = location
    inbox, vault = value["inbox"].resolve(strict=True), value["vault"].resolve(strict=True)
    if inbox == vault or inbox in vault.parents or vault in inbox.parents:
        raise ValueError("inbox and vault must be disjoint")
    if 'local_only' in value and type(value['local_only']) is not bool:
        raise ValueError('local_only must be a boolean')
    if value.get('legacy_db'):
        legacy = Path(value['legacy_db'])
        if not legacy.is_absolute() or legacy.is_symlink() or not legacy.is_file():
            raise ValueError('invalid legacy database')
        value['legacy_db'] = legacy
    value["state"] = base
    return value


def approvals(cfg):
    if cfg.get('local_only'):
        return set()
    path = cfg["approvals"]
    if not path.is_file():
        return set()
    info = path.stat()
    if path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("insecure approvals file")
    data = read_json(path)
    if data.get("destination") != "OpenAI Codex" or not isinstance(data.get("sha256"), list):
        raise ValueError("invalid approvals")
    if any(not isinstance(sha, str) or not DIGEST.fullmatch(sha) for sha in data["sha256"]):
        raise ValueError("invalid approved digest")
    return set(data["sha256"])


def afr(cfg, command, *args, timeout):
    try:
        run = subprocess.run([str(cfg["python"]), "-B", "-m", "afr_agent", command, *map(str, args)],
                             cwd=cfg["cwd"], timeout=timeout, capture_output=True)
        data = json.loads(run.stdout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "subprocess_timeout"}
    except (OSError, ValueError):
        return {"ok": False, "error": "io_error"}
    return data if isinstance(data, dict) and data.get("version") == 1 else {"ok": False, "error": "io_error"}


def fingerprint(directory):
    stamp = hashlib.sha256()
    for parent, dirs, files in os.walk(directory):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not Path(parent, d).is_symlink())
        for name in sorted(files):
            path = Path(parent, name)
            if name.startswith((".", "~$")) or path.is_symlink():
                continue
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            stamp.update(os.fsencode(path.relative_to(directory)) + b"\0")
            stamp.update(f"{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}\n".encode())
    return stamp.digest()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post(cfg, sha, attempt, generation):
    url = urlparse(cfg["source_url"])
    if url.scheme != "http" or url.hostname != "127.0.0.1" or url.path != "/v1/in/afr-drop":
        raise ValueError("expected fixed loopback AFR source")
    secret = Path(cfg["source_secret"]).read_bytes().strip()
    data = json.dumps({"sha256": sha, "attempt": attempt, "generation": generation}).encode()
    signature = hmac.new(secret, data, hashlib.sha256).hexdigest()
    req = Request(cfg["source_url"], data=data, headers={"Content-Type":"application/json", "X-AFR-Signature":signature})
    with build_opener(ProxyHandler({}), NoRedirect()).open(req, timeout=5) as response:
        return json.load(response)


def ledger(cfg, sha):
    path = cfg["state"] / (sha + ".json")
    return path, read_json(path) if path.exists() else {}


def next_attempt(cfg, sha):
    _, last = ledger(cfg, sha)
    attempt = last.get("attempt", 0) + 1
    if attempt <= len(RETRY_DELAYS) and last.get("state") not in ("published", "blocked") and last.get("retry_after", 0) <= time.time():
        return attempt, last.get("generation", "initial")
    return None


def scan(cfg):
    before = fingerprint(cfg["inbox"])
    legacy = ["--legacy-db", cfg["legacy_db"]] if cfg.get("legacy_db") else []
    result = afr(cfg, "scan", "--inbox", cfg["inbox"], "--vault", cfg["vault"], *legacy,
                 "--settle-seconds", SETTLE_SECONDS, timeout=180)
    if not result.get("ok"):
        error = result.get("error")
        return {"state": "scan_failed", "code": error if error in SCAN_CODES else "invalid_result"}
    pending = {row["sha256"] for row in result["pending"]}
    unsettled = result.get("settling", 0)
    changed = fingerprint(cfg["inbox"]) != before
    errors = len(result["errors"])
    codes = {}
    for item in result["errors"]:
        code = item.get("error") if isinstance(item, dict) else None
        code = code if isinstance(code, str) and code in SCAN_CODES else "local_read_error"
        codes[code] = codes.get(code, 0) + 1
    details = {"scan_errors": errors, "error_counts": codes,
               "unsupported_count": len(result.get("unsupported", []))}
    if changed:
        return {"state": "settling", "new_count": len(result["added"]), "pending_count": len(pending),
                "settling_count": unsettled, **details, "submitted_count": 0}
    authorization_errors = 0
    try:
        eligible = pending.intersection(approvals(cfg))
    except (OSError, TypeError, ValueError):
        eligible = set()
        authorization_errors = 1
    routed = 0
    undelivered = 0
    for sha in sorted(eligible):
        assignment = next_attempt(cfg, sha)
        if assignment is None:
            continue
        try:
            post(cfg, sha, *assignment)
        except (OSError, ValueError):
            undelivered += 1
        else:
            routed += 1
    return {"state": "scanned", "new_count": len(result["added"]), "pending_count": len(pending),
            **details, "settling_count": unsettled, "allowed_count": len(eligible), "submitted_count": routed,
            "submission_errors": undelivered, "authorization_errors": authorization_errors}


@contextmanager
def lock_file(path):
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True


def process(cfg, detail):
    sha, attempt, generation = (detail.get(key) for key in ("sha256", "attempt", "generation"))
    if not isinstance(sha, str) or not DIGEST.fullmatch(sha) or type(attempt) is not int or not 1 <= attempt <= len(RETRY_DELAYS):
        return {"state": "invalid_assignment"}
    with lock_file(cfg["state"] / (sha + ".lock")) as acquired:
        if not acquired:
            return {"state": "in_progress", "sha256": sha}
        path, last = ledger(cfg, sha)
        if last.get("state") in ("published", "blocked") or (attempt, generation) != next_attempt(cfg, sha):
            return {"state": "replayed", "sha256": sha}
        try:
            authorized = sha in approvals(cfg)
        except (OSError, TypeError, ValueError):
            authorized = False
        if not authorized:
            # 已进 Hub 的尝试可能在领取前被撤权。恢复同一版本许可后使用新键。
            write_json(path, {"state": "authorization_required", "attempt": attempt, "generation": generation})
            return {"state": "not_authorized", "sha256": sha, "attempt": attempt}
        # 崩溃后等待原过程退出。AFR process 自己也按固定版本锁定整轮。
        write_json(path, {"state": "running", "attempt": attempt, "generation": generation, "retry_after": time.time() + 900})
        # 模型与 AFR 保持在原生 Worker 的进程组内，超时由中台统一停止。
        result = afr(cfg, "process", "--vault", cfg["vault"], "--sha256", sha,
                     "--approvals", cfg["approvals"], timeout=None)
        raw = result.get("error")
        if result.get("ok") and result.get("sha256") == sha and (
                result.get("state") == "already_published" or type(result.get("topics")) is int):
            status, code = "published", result.get("state", "published")
        elif raw in TRANSIENT and attempt < len(RETRY_DELAYS):
            status, code = "retry", raw
        else:
            status, code = "blocked", raw if raw in TRANSIENT | STOPPED else "invalid_result"
        write_json(path, {"state": status, "attempt": attempt, "generation": generation, "code": code,
                          "retry_after": time.time() + RETRY_DELAYS[attempt-1]})
        return {"state": status, "sha256": sha, "attempt": attempt, "code": code}


def rearm(cfg, sha):
    if not DIGEST.fullmatch(sha or "") or sha not in approvals(cfg):
        raise ValueError("explicit approved digest required")
    with lock_file(cfg["state"] / (sha + ".lock")) as acquired:
        if not acquired:
            raise ValueError("active job")
        path, last = ledger(cfg, sha)
        if last.get("state") == "published":
            raise ValueError("already published")
        if last.get("state") == "running" and last["retry_after"] > time.time():
            raise ValueError("previous attempt may still be running")
        write_json(path, {"state": "armed", "generation": secrets.token_hex(8), "attempt": 0})
    return {"state": "armed", "sha256": sha}


def watch(cfg):
    previous = fingerprint(cfg["inbox"])
    last_scanned = previous
    changed_at = time.monotonic()
    next_recovery = 0
    with lock_file(cfg["state"] / "observer.lock") as acquired:
        if not acquired:
            raise ValueError("observer already running")
        while True:
            now = time.monotonic()
            current = fingerprint(cfg["inbox"])
            if current != previous:
                previous, changed_at = current, now
            stable_change = current != last_scanned and now - changed_at >= SETTLE_SECONDS
            if stable_change or now >= next_recovery:
                report = scan(cfg)
                print(json.dumps({"unix_time": time.time(), **report}, ensure_ascii=False), flush=True)
                last_scanned = current
                retry_soon = report.get("settling_count") or report["state"] == "settling"
                next_recovery = time.monotonic() + (SETTLE_SECONDS if retry_soon else RECOVERY_SECONDS)
            time.sleep(POLL_SECONDS)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("watch", "scan", "process", "rearm"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--sha256")
    args = parser.parse_args()
    try:
        cfg = config(args.config)
        if args.mode == "watch":
            watch(cfg)
            return
        if args.mode == "scan":
            print(json.dumps(scan(cfg), ensure_ascii=False))
            return
        if args.mode == "rearm":
            print(json.dumps(rearm(cfg, args.sha256)))
            return
        context = read_json(Path(os.environ["AGENT_AUTOLOOP_CONTEXT"]))
        results = [process(cfg, json.loads(row["detail"])) for row in context["events"]
                   if row.get("source") == "afr-drop" and row.get("type") == "afr.intake.pending"]
    except (OSError, KeyError, TypeError, ValueError):
        results = [{"state": "local_configuration_or_io_error"}]
    print("AFR " + json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main()
