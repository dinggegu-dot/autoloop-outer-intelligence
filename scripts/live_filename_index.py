"""从隔离正式入口验收合成文件名索引，并在已执行阶段注入一次回归。"""

import argparse
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import ExitStack
from http.cookies import SimpleCookie
from pathlib import Path

SEARCH = ["alpha", "alphabet.md", "beta.txt", "alpha.txt"]
UPDATE = ["beta.txt,alpha.txt", "alpine.md", "beta.txt"]
SEARCH_RESULT = {"matches": ["alpha.txt", "alphabet.md"]}
UPDATE_RESULT = {"filenames": ["alpha.txt", "alpine.md"]}
ORDER = {
    "workflow": "outer",
    "name": "合成文件名查询与增量索引",
    "goal": (
        "交付 search.py 和 update.py 两个命令行工具。search.py 的第一个参数是文件名前缀，"
        "其余参数是文件名，按文件名升序输出包含命中数组的 JSON。"
        "update.py 的三个参数分别是已有文件名、新增文件名、删除文件名，均用逗号分隔。"
        "返回包含更新后文件名数组的 JSON，并按文件名升序输出。"
    ),
    "acceptance": (
        'search.py alpha alphabet.md beta.txt alpha.txt 输出 {"matches":["alpha.txt","alphabet.md"]}。'
        'search.py z beta.txt alpha.txt 输出 {"matches":[]}。'
        'update.py beta.txt,alpha.txt alpine.md beta.txt 输出 {"filenames":["alpha.txt","alpine.md"]}。'
        'update.py zebra.md,old.md apple.md,zebra.md old.md 输出 {"filenames":["apple.md","zebra.md"]}。'
        '可先把 search.py 作为一个有验收条件的独立实施步骤，再实现 update.py。'
    ),
    "request_id": "11111111-2222-4333-8444-101010101010",
}
TOOL_NAMES = {
    "read_failure_evidence", "read_verified_artifacts", "read_plan_versions", "read_remaining_budget",
}
EXPECTED = {
    "search.py": (SEARCH, SEARCH_RESULT),
    "update.py": (UPDATE, UPDATE_RESULT),
}


def save_json(path, value):
    staging = path.with_suffix(".next")
    staging.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    staging.replace(path)


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def wait_until(callback, timeout, reason):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = callback()
        if result:
            return result
        time.sleep(0.5)
    raise RuntimeError(reason)


def loopback_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Board:
    def __init__(self, port):
        self.url = f"http://127.0.0.1:{port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.headers = {"Origin": self.url, "Content-Type": "application/json"}
        session, headers = self.call("/api/session", headers=True)
        cookie = SimpleCookie(headers["Set-Cookie"])
        token = cookie[f"odyssey_session_{port}"]
        self.headers.update(Cookie=f"{token.key}={token.value}", **{"X-Odyssey-CSRF": session["csrf"]})

    def call(self, path, body=None, *, status=200, headers=False, raw=False):
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        request = urllib.request.Request(self.url + path, data=payload, headers=self.headers)
        try:
            response = self.opener.open(request, timeout=4)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            value = response.read()
            if response.status != status:
                raise RuntimeError(f"{path}: HTTP {response.status}, expected {status}: {value[:400]!r}")
            value = value if raw else json.loads(value)
            return (value, response.headers) if headers else value


class Isolated:
    def __init__(self, code, root, stack):
        self.root, self.stack = root, stack
        self.app = code
        self.runtime = root / "workbench"
        self.hub = root / "hub"
        self.worker = root / "worker"
        self.port, self.hub_port = loopback_port(), loopback_port()
        require(self.port != self.hub_port and 8765 not in (self.port, self.hub_port), "隔离端口冲突")
        for path in (self.runtime, self.hub, self.worker, root / "cwd", root / "tmp"):
            path.mkdir(mode=0o700)
        self.env = {key: os.environ[key] for key in (
            "PATH", "HOME", "CODEX_HOME", "LANG", "LC_ALL", "LC_CTYPE", "AUTOLOOP_OUTER_PROXY_PORT"
        ) if key in os.environ}
        self.env.update(TMPDIR=str(root / "tmp"), SHELL="/bin/sh")
        install = Path.home() / ".agent-autoloop"
        node = install / "runtime/node-v24.22.2/bin/node"
        entry = install / "app/bin/agent-autoloop.ts"
        require(node.is_file() and entry.is_file(), "本机 Autoloop 安装入口缺失")
        self.cli = [str(node), "--disable-warning=ExperimentalWarning", str(entry)]
        (root / ".filename-index-pilot").touch(mode=0o600)
        (root / ".goal-pilot").write_text(str(self.hub_port) + "\n")

    def start(self, command, name, env=None):
        with (self.root / (name + ".log")).open("ab") as log:
            process = subprocess.Popen(command, cwd=self.root / "cwd", start_new_session=True,
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                       env=self.env if env is None else env)
        self.stack.callback(self.stop, process)
        return process

    @staticmethod
    def stop(process):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=10)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)

    def instance_env(self, instance):
        return dict(self.env, AGENT_AUTOLOOP_HOME=str(instance))

    def launch(self, mode):
        (self.root / "mode").write_text(mode)
        worker_driver = self.root / "worker-driver.py"
        worker_driver.write_bytes(Path(__file__).read_bytes())
        command = [sys.executable, "-B", str(worker_driver), "--isolated-worker",
                   str(self.app), str(self.runtime)]
        save_json(self.hub / "rules.json", {
            "sources": {"local": {"name": "Local"}}, "channels": {},
            "agents": {"odyssey-goal": {"name": "Odyssey Goal", "worker": "local", "use": "command",
                                        "timeout": 6, "command": command}},
            "rules": [{"name": "odyssey-goal", "when": {"source": "local", "types": ["workbench.goal.assignment"]},
                       "to": "odyssey-goal", "cwd": str(self.root / "cwd"),
                       "prompt": "Process the Workbench goal assignment.", "parallel": 1, "tell": "never"}],
        })
        keys = {}
        for kind, name in (("owner", "owner"), ("source", "local"), ("worker", "local")):
            result = subprocess.run([*self.cli, "key", "new", kind, name], env=self.instance_env(self.hub),
                                    check=True, capture_output=True, text=True, timeout=15)
            keys[kind] = result.stdout.strip()
        hub_url = f"http://127.0.0.1:{self.hub_port}"
        save_json(self.hub / "config.json", {
            "url": hub_url, "hub": {"listen": f"127.0.0.1:{self.hub_port}"},
            "owner": keys["owner"], "source": {"name": "local", "key": keys["source"]},
        })
        save_json(self.worker / "config.json", {
            "url": hub_url, "worker": {"name": "local", "key": keys["worker"], "capacity": 1},
        })
        self.start([*self.cli, "up"], "hub", self.instance_env(self.hub))
        wait_until(lambda: self.ready(hub_url + "/healthz"), 15, "隔离 Hub 就绪超时")
        self.worker_process = self.start_worker()
        self.start([sys.executable, "-B", str(self.app / "server.py"), "--port", str(self.port),
                    "--runtime", str(self.runtime), "--hub-home", str(self.hub), "--goal-pilot",
                    "--outer-intelligence", "--outer-model-timeout", "120"], "board")
        wait_until(lambda: self.ready(f"http://127.0.0.1:{self.port}/api/session"), 15, "隔离工作台就绪超时")
        board = Board(self.port)
        page = board.call("/", raw=True)
        require(b'id="outer-opt-in"' in page, "正式工作台普通表单缺少显式试点选项")
        return board

    @staticmethod
    def ready(url):
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=1) as response:
                return response.status == 200
        except OSError:
            return False

    def start_worker(self):
        return self.start([*self.cli, "up"], "worker", self.instance_env(self.worker))

    def restart_worker(self):
        old = self.worker_process.pid
        self.stop(self.worker_process)
        self.worker_process = self.start_worker()
        return [old, self.worker_process.pid]


def worker(app, runtime):
    require(runtime.parent.parent in (Path("/private/tmp"), Path("/tmp")) and
            (runtime.parent / ".filename-index-pilot").is_file() and runtime.name == "workbench",
            "故障注入仅允许在本验收新建的隔离目录")
    sys.path.insert(0, str(app))
    from superagent import goal_runtime
    from superagent.outer_verification import capture
    from workbench import Store

    execute = goal_runtime.execute_assignment
    mode = (runtime.parent / "mode").read_text()

    def regress(store, assignment, directory, verification):
        report = execute(store, assignment, directory, verification)
        mark = runtime.parent / "fault.json"
        if (assignment["phase"] == "execute" and assignment["step"] == 1 and
                not report.get("error") and not mark.exists()
                and mode in {"regression", "pause-stale"}):
            workspace = store.workspace(assignment["ticket"])
            search = workspace / "search.py"
            old = hashlib.sha256(search.read_bytes()).hexdigest()
            search.write_text('print(\'{"matches":["regression.txt"]}\')\n')
            report["checkpoint"] = capture(workspace)[0]
            save_json(mark, {"assignment": assignment["id"], "run": assignment["run"],
                             "path": "search.py", "before": old,
                             "after": hashlib.sha256(search.read_bytes()).hexdigest(),
                             "checkpoint": report["checkpoint"]["digest"],
                             "unaffected_update_sha256": report["checkpoint"]["files"]["update.py"]})
            if mode == "regression":
                wait_until((runtime.parent / "restart-release").exists, 30, "未观察到执行回执前的 Worker 重启")
                # 真实执行已结束，隔离夹具在业务回执落盘前丢失它。原生输送层仍能结束此轮。
                print("没事可做：隔离夹具模拟业务回执未提交", flush=True)
                raise SystemExit(0)
        if mode == "pause-stale" and assignment["phase"] == "coordinate" and not report.get("error"):
            save_json(runtime.parent / "pause-barrier.json",
                      {"assignment": assignment["id"], "run": assignment["run"], "result": report["result"]})
            wait_until((runtime.parent / "pause-release").exists, 30, "未观察到工单暂停")
        return report

    goal_runtime.execute_assignment = regress
    goal_runtime.worker(Store(runtime), None)


def validate_contract(draft):
    criteria = draft["acceptance_contract"]["criteria"]
    scripts = {name: set() for name in EXPECTED}
    explicit = set()
    for row in criteria:
        spec = row["test_spec"]
        script = spec.get("script")
        require(row["verifier_id"] == "python-json-cli" and script in EXPECTED and not spec["files"],
                "模型提出的验收超出只允许两个合成命令的范围")
        args = spec["args"]
        require(all(re.fullmatch(r"[A-Za-z0-9._,-]{0,120}", arg) for arg in args),
                "验收参数超出合成文件名范围")
        if script == "search.py":
            require(args, "查询用例需要前缀")
            result = {"matches": sorted(name for name in args[1:] if name.startswith(args[0]))}
            require("regression.txt" not in result["matches"], "合成查询用了预留的故障占位名")
        else:
            require(len(args) == 3, "增量索引用例需要三个参数")
            existing, added, removed = (set(field.split(",")) - {""} for field in args)
            result = {"filenames": sorted((existing | added) - removed)}
        require(row["expected_result"] == result, "合成用例的期望值与具体输入不符")
        scripts[script].add(row["criterion_id"])
        if row["required"] and args == EXPECTED[script][0]:
            explicit.add(script)
    require(explicit == set(EXPECTED), "合成审批缺少至少一项请求中的固定用例")
    return {name: sorted(items) for name, items in scripts.items()}


def submit(board):
    posted = board.call("/api/tasks", ORDER, status=201)
    ticket = posted.get("ticket_id")
    event_id = posted["event_id"]

    def draft_ready():
        event = board.call("/api/outer/event", {"event_id": event_id})
        current = ticket or event.get("ticket_id")
        if not current:
            require(event["state"] not in {"DONE", "NEEDS_ATTENTION"},
                    f"接单没有生成工单：{event.get('error', event['state'])}")
            return None
        draft = board.call("/api/outer/get", {"ticket_id": current})
        if draft["status"] == "AWAITING_APPROVAL":
            return draft
        require(draft["status"] not in {"NEEDS_ATTENTION", "VERIFICATION_GAP", "AWAITING_CLARIFICATION"},
                f"合成工单未达到可批准状态 {draft['status']}")
        return None

    return wait_until(draft_ready, 280, "等待接单与验收契约超时"), posted


def verify_call_trace(board, task, proposal):
    ticket = task["id"]
    assignment = proposal["assignment"]
    calls = board.call("/api/outer/calls", {"ticket_id": ticket})
    indexed = [row for row in calls if row["id"] == assignment and row["role"] == "goal_coordinator"]
    require(len(indexed) == 1, "工单调用目录未唯一关联真实协调调用")
    detail = board.call("/api/outer/call", {"ticket_id": ticket, "call_id": assignment})
    require(detail["output"] == proposal["proposal"], "调用详情未展示原模型提案")
    require({item["name"] for item in detail["tools"] if item["status"] == "completed"} == TOOL_NAMES,
            "调用详情未展示四次真实 MCP 工具读取")
    trace = detail["trace"]
    require(trace["disposition"] == proposal["disposition"] and trace["run"] == proposal["run"],
            "调用详情未与协调处置和中台轮次绑定")
    if proposal["disposition"] == "stale":
        require(indexed[0]["state"] == "stale" and trace["reason"] and trace["decision"] is None
                and not trace["subsequent_runs"] and not trace["dispatch"], "暂停后的过期调用包含后续执行")
    else:
        require(trace["decision"] == proposal["proposal"] and trace["decision"]["reason"]
                and trace["dispatch"] == proposal["dispatch"], "调用详情缺采纳原因或后续派工")
        delivery = task["goal_delivery"]
        require(trace["decision"]["affected_steps"] == [0, 1], "提案遗漏了已发生回归的前置阶段")
        rows = trace["subsequent_runs"]
        require(rows and rows[0]["assignment"] == trace["dispatch"]
                and rows[-1]["assignment"] == delivery["assignment"] and rows[-1]["phase"] == "review"
                and rows[-1]["passed"] and rows[-1]["delivery_digest"] == delivery["artifact_sha256"],
                "调用详情未展示已采纳修订到终验的整段执行")
        assignments = {row["id"]: row for row in task["goal_execution"]["assignments"]}
        require(all(assignments[row["assignment"]]["outer_binding"] == proposal["binding"]
                    and assignments[row["assignment"]]["run"] == row["run"] for row in rows),
                "修复与复验轮次没有沿用相同授权")
        retained = {item["path"]: item["sha256"] for item in trace["decision"]["retained_artifacts"]}
        require(retained.get("update.py") == delivery["files"]["update.py"], "提案未明确保留通过验收的增量索引版本")
    return {"call": assignment, "tools": detail["tools"], "trace": trace}


def inspect_run(root):
    require(root.parent == Path("/private/tmp") and (root / ".filename-index-pilot").is_file(),
            "只读复核须指向本验收创建的隔离运行目录")
    report = json.loads((root / "acceptance.json").read_text())
    require(report["passed"] and report["mode"] in {"regression", "pause-stale"}, "仅复核已结束的协调试验")
    app = root / "formal-board"
    sys.path.insert(0, str(app))
    from workbench import Store

    store = Store(root / "workbench")
    with store.connect() as db:
        (ticket,) = db.execute("SELECT id FROM tasks").fetchone()
        require(db.execute("SELECT count(*) FROM outer_events WHERE role IS NOT NULL AND state IN ('PENDING','RUNNING')")
                .fetchone()[0] == 0, "仍有模型请求，暂缓复核")
    task = store.get(ticket)
    (proposal,) = task["goal_execution"]["coordination_proposals"].values()
    port = loopback_port()
    require(port != 8765, "复核端口不能与正式入口相同")
    with (root / "trace-inspection.log").open("ab") as log:
        process = subprocess.Popen(
            [sys.executable, "-B", str(app / "server.py"), "--port", str(port),
             "--runtime", str(store.root), "--hub-home", str(root / "hub"), "--goal-pilot", "--outer-intelligence"],
             cwd=root / "cwd", start_new_session=True, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            wait_until(lambda: Isolated.ready(f"http://127.0.0.1:{port}/api/session"), 10, "隔离复核服务未就绪")
            result = verify_call_trace(Board(port), task, proposal)
            save_json(root / "trace-acceptance.json", result)
        finally:
            Isolated.stop(process)


def execute_trial(pilot, board, evidence, mode):
    sys.path.insert(0, str(pilot.app))
    from superagent import goal_checks, goal_runtime
    from workbench import Hub, Store

    draft, posted = submit(board)
    ticket = draft["id"]
    evidence["ticket"] = ticket
    criteria = validate_contract(draft)
    evidence["criteria"] = criteria
    binding = {key: draft[key] for key in ("goal_revision", "acceptance_revision", "revision")}
    approval = dict(binding, ticket_id=ticket, request_id="filename-index-approve")
    board.call("/api/outer/approve", dict(approval, revision=binding["revision"] + 1), status=409)
    evidence["wrong_approval_version_rejected"] = True
    board.call("/api/outer/approve", approval)
    evidence["approved"] = binding

    store = Store(pilot.runtime)
    hub = Hub(pilot.hub)
    runtime = goal_runtime.GoalRuntime(store, hub)
    failure = None
    evaluated_attempts = set()
    stale = None
    paused = False
    restarted = False
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        fault_marker = pilot.root / "fault.json"
        if mode == "regression" and not restarted and fault_marker.is_file():
            fault = json.loads(fault_marker.read_text())
            task = store.get(ticket)
            execute = next(row for row in task["goal_execution"]["assignments"] if row["id"] == fault["assignment"])
            path = goal_runtime.attempt_file(store.root, ticket, execute["id"], "report.json")
            (transport,) = hub.request("GET", f"/v1/events/{execute['event']}", source=True)["runs"]
            require(transport["run"] == fault["run"] and transport["state"] == "claimed" and not path.exists(),
                    "Worker 重启之前必须观察到已领的执行轮次且工作回执尚未落盘")
            evidence["worker_restart"] = {"pids": pilot.restart_worker(), "assignment": execute["id"],
                                          "run": fault["run"], "prior_transport": transport["state"],
                                          "prior_report": False}
            (pilot.root / "restart-release").touch()
            restarted = True
            replay = board.call("/api/tasks", ORDER, status=201)
            require(replay["event_id"] == posted["event_id"] and replay.get("duplicate") is True,
                    "重启 Worker 后重放普通工单生成了新接单")
            evidence["order_duplicate"] = True
            repeated = board.call("/api/outer/approve", approval)
            require(repeated.get("duplicate") is True and repeated["mandate_revision"] == 1,
                    "重放相同审批生成了第二次授权")
            evidence["approval_duplicate"] = True
        if mode == "pause-stale" and failure is not None and stale is None:
            task = store.get(ticket)
            barrier = pilot.root / "pause-barrier.json"
            if not paused and barrier.is_file():
                released = json.loads(barrier.read_text())
                current = board.call("/api/outer/get", {"ticket_id": ticket})
                board.call("/api/outer/pause", {"ticket_id": ticket, "revision": current["revision"],
                                               "request_id": "filename-index-pause"})
                (pilot.root / "pause-release").touch()
                paused = True
            if paused:
                path = goal_runtime.attempt_file(store.root, ticket, released["assignment"], "report.json")
                if path.is_file():
                    stale = json.loads(path.read_text())
                    require(stale.get("result") == released["result"] and stale.get("run") == released["run"],
                            "暂停后收到的回执与原 Codex 提案不一致")
            active = next((row for row in reversed(task["goal_execution"]["assignments"])
                           if row["phase"] == "coordinate" and row["run"]), None)
            if active and not paused:
                receipt = goal_runtime.attempt_file(store.root, ticket, active["id"], "report.json")
                if receipt.is_file():
                    error = json.loads(receipt.read_text()).get("error", "未触发暂停屏障")
                    raise RuntimeError(f"未生成可暂停的协调提案：{error}")
            if active and not stale:
                time.sleep(.4)
                continue
        task = runtime.tick(ticket)
        state = task["goal_execution"]
        validation = state.get("validation") or {}
        rows = validation.get("checks", [])
        outcomes = {row["id"]: row["status"] for row in rows}
        if validation.get("level") == "stage" and outcomes == dict.fromkeys(criteria["search.py"], "PASS"):
            delivery, = state["stage_deliveries"]
            archive = goal_runtime.attempt_file(store.root, ticket, delivery["assignment"], "verified") / "search.py"
            require(hashlib.sha256(archive.read_bytes()).hexdigest() == delivery["files"]["search.py"],
                    "前序阶段归档与验收清单不一致")
            evidence["stage_one"] = {"checks": outcomes, "artifact_sha256": delivery["artifact_sha256"]}
        if state["stage"] == "coordinate" and failure is None and rows:
            require(outcomes == (dict.fromkeys(criteria["search.py"], "FAIL") | dict.fromkeys(criteria["update.py"], "PASS")),
                    f"故障并非仅影响先前已通过验收项 {outcomes}")
            fault = json.loads((pilot.root / "fault.json").read_text())
            require(validation["artifact_sha256"] == fault["checkpoint"], "回归证据未绑定注入后的工作区")
            failure = validation["artifact_sha256"]
            failed_review = state["coordination"]["assignment"]
            require(not task.get("goal_delivery"), "未通过复验的工作区进入了已发布交付")
            path = f"/api/tasks/{ticket}/artifact?path=search.py"
            board.call(path, status=404)
            board.call(f"{path}&candidate={failed_review}", status=404)
            evidence["regression"] = {
                "checks": outcomes, "fault": fault, "failed_review": failed_review,
                "wrong_candidate_download_status": 404,
            }
            if mode == "regression":
                require(restarted and any(row["kind"] == "recovered_artifacts" and row.get("digest") == failure
                                          for row in state["decisions"]), "重启后未从丢失回执的运行现场恢复检查点")
        for assignment in state["assignments"]:
            if assignment["id"] in evaluated_attempts or assignment["phase"] != "coordinate":
                continue
            path = goal_runtime.attempt_file(store.root, ticket, assignment["id"], "report.json")
            if path.is_file():
                report = json.loads(path.read_text())
                model_evidence = dict(report.get("evidence", {}))
                session = model_evidence.pop("session_id", None)
                model_evidence.pop("session", None)
                if session:
                    model_evidence["session_sha256"] = hashlib.sha256(session.encode()).hexdigest()
                evidence.setdefault("coordination", []).append(
                    {key: report.get(key) for key in ("id", "run", "version", "result", "error")}
                    | {"evidence": model_evidence})
                evaluated_attempts.add(assignment["id"])
        if stale is not None and task["status"] == "paused":
            require(not any(row["phase"] in {"repair", "replan"} for row in state["assignments"]),
                    "暂停后过期提案派出了实施轮次")
            require(len(state["stage_deliveries"]) == 1 and not task.get("goal_delivery"),
                    "暂停后过期提案改写了交付")
            proposal = state.get("coordination_proposals", {}).get(stale["id"], {})
            require(proposal.get("disposition") == "stale" and proposal.get("proposal") == stale["result"]
                    and not proposal.get("dispatch"), "已暂停的提案没有按过期状态留档")
            evidence["call_trace"] = verify_call_trace(board, task, proposal)
            evidence["paused_stale_proposal"] = {"assignment": stale["id"], "run": stale["run"],
                                                  "status": task["status"], "repair_count": 0}
            return
        if task["status"] in {"blocked", "budget_exhausted", "paused", "rejected", "awaiting_artifact_review"}:
            raise RuntimeError(f"执行未交付 {task['status']} / {state['stage']} / {task.get('error', '')}")
        if task["status"] == "completed":
            require(evidence.get("stage_one"), "缺阶段一正常交付证据")
            if mode == "regression":
                require(failure and evidence.get("coordination"), "缺故障及真实协调证据")
                ledger = state.get("coordination_proposals", {})
                accepted = [row for row in ledger.values() if row["disposition"] == "accepted"
                            and row["proposal"]["action"] in {"REPAIR", "REPLAN"}]
                require(len(accepted) == 1, "回归应采用一次可执行的协调提案")
                proposal = accepted[0]
                tools = proposal["invocation"]["tools"]
                require({item["name"] for item in tools if item["status"] == "completed"} == TOOL_NAMES,
                        "协调提案没有完成四次真实证据工具读取")
                require(sum(a.get("coordination_proposal") == proposal["assignment"]
                            for a in state["assignments"]) == 1, "同一提案重复生成后续派工")
                coord_report = next(a["report"] for a in state["assignments"] if a["id"] == proposal["assignment"])
                replay = goal_runtime.GoalEngine(Store(pilot.runtime)).settle(
                    ticket, proposal["assignment"], proposal["run"], "done", coord_report
                )
                require(replay["goal_execution"]["assignments"] == state["assignments"]
                        and replay["goal_execution"]["coordination_proposals"] == ledger,
                        "持久化模型回执重放改变了已登记派工或提案")
                evidence["call_trace"] = verify_call_trace(board, task, proposal)
                evidence["proposal_duplicate"] = {
                    "assignment": proposal["assignment"], "run": proposal["run"], "dispatch": proposal["dispatch"],
                    "tools": tools,
                }
            else:
                require(not failure and not evidence.get("coordination"), "正常对照意外触发了协调")
            require(outcomes == dict.fromkeys((key for items in criteria.values() for key in items), "PASS"), "终验没有复跑全部条件")
            delivery = task["goal_delivery"]
            require(failure != delivery["artifact_sha256"], "交付仍指向回归产物")
            if mode == "regression":
                original_update = evidence["regression"]["fault"]["unaffected_update_sha256"]
                require(delivery["files"]["update.py"] == original_update, "修复改动了已通过验收的增量接口版本")
            for filename in EXPECTED:
                data = board.call(f"/api/tasks/{ticket}/artifact?path={urllib.parse.quote(filename)}", raw=True)
                require(hashlib.sha256(data).hexdigest() == delivery["files"][filename], "下载字节不匹配归档")
            archive = goal_runtime.attempt_file(store.root, ticket, delivery["assignment"], "verified")
            unseen = goal_checks.verify(archive, {"id": "new-inputs", "checks": [
                {"id": "unseen-search", "script": "search.py", "files": {},
                 "args": ["cap", "map-cap.md", "captain.txt", "cape.csv"],
                 "expected": {"matches": ["cape.csv", "captain.txt"]}},
                {"id": "unseen-update", "script": "update.py", "files": {},
                 "args": ["ledger.md,note.txt", "ledger.md,appendix.txt", "note.txt,absent.md"],
                 "expected": {"filenames": ["appendix.txt", "ledger.md"]}},
            ]})
            require(unseen["passed"], f"归档在独立合成输入下未通过 {unseen['checks']}")
            evidence["unseen_inputs"] = unseen["checks"]
            final_draft = board.call("/api/outer/get", {"ticket_id": ticket})
            require(final_draft["mandate_revision"] == 1, "试点必须沿用唯一一次批准")
            evidence["delivery"] = {"checks": outcomes, "artifact_sha256": delivery["artifact_sha256"],
                                    "mandate_revision": final_draft["mandate_revision"]}
            return
        time.sleep(.4)
    raise RuntimeError("隔离文件名索引执行超时")


def deployed_listener_pids():
    result = subprocess.run(["/usr/sbin/lsof", "-t", "-nP", "-iTCP:8765", "-sTCP:LISTEN"],
                            capture_output=True, text=True, timeout=4)
    require(result.returncode in {0, 1}, "无法核对正式监听进程")
    return sorted(int(value) for value in result.stdout.split())


def source_proof(formal, isolated):
    source_files = ("server.py", "tasks.js", "index.html", "workbench.py")
    signatures = {name: hashlib.sha256((formal / name).read_bytes()).hexdigest() for name in source_files}
    all_paths = list(isolated.rglob("*"))
    forbidden = {"afr-handoff.local.json", "runtime-location", ".runtime", "master.key", "keys",
                 "auth.json", "config.json", "hub.db", "snapshot.json"}
    leaked = [str(path.relative_to(isolated)) for path in all_paths
              if path.name in forbidden or path.name.endswith((".key", ".sqlite", ".sqlite3", "-wal", "-shm"))]
    require(not leaked, f"正式源码副本包含不应复制的本机状态 {leaked}")
    copied = {name: hashlib.sha256((isolated / name).read_bytes()).hexdigest() for name in source_files}
    return {"original_sha256": signatures, "copied_sha256": copied, "copied_paths_examined": len(all_paths),
            "runtime_credentials_local_config_absent": True}


def main():
    os.umask(0o077)

    def stop_requested(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_requested)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, default=Path(__file__).resolve().parents[1],
                        help="候选代码仓库")
    parser.add_argument("--formal-source", type=Path,
                        help="正式工作台源码目录，由候选脚本复制并剔除运行状态")
    parser.add_argument("--mode", choices=("control", "regression", "pause-stale", "launch-only"),
                        default="regression")
    parser.add_argument("--inspect-run", type=Path, help="通过工作台调用接口复核已完成的隔离协调记录")
    args = parser.parse_args()
    if args.inspect_run is not None:
        inspect_run(args.inspect_run.resolve())
        return
    if args.formal_source is None:
        parser.error("新试验须指定 --formal-source")
    root = Path(tempfile.mkdtemp(prefix="autoloop-filename-index-", dir="/private/tmp"))
    print(root, flush=True)
    before = deployed_listener_pids()
    evidence = {"synthetic": True, "entry": "POST /api/tasks", "mode": args.mode, "passed": False,
                "formal_listener_pids_before": before}
    try:
        with ExitStack() as stack:
            candidate = args.candidate.resolve()
            board_source = root / "formal-board"
            formal = args.formal_source.resolve()
            subprocess.run([sys.executable, "-B", str(candidate / "scripts/prepare_formal_outer_pilot.py"),
                            str(formal), str(board_source)], check=True, timeout=40)
            evidence["formal_source"] = source_proof(formal, board_source)
            files = (
                "server.py", "outer_workbench.py", "superagent/goal_engine.py",
                "superagent/goal_runtime.py", "superagent/outer_model.py", "superagent/coordination_tools.py",
            )
            evidence["source_sha256"] = {
                name: hashlib.sha256((board_source / name).read_bytes()).hexdigest() for name in files
            }
            pilot = Isolated(board_source, root, stack)
            board = pilot.launch(args.mode)
            if args.mode != "launch-only":
                execute_trial(pilot, board, evidence, args.mode)
            evidence["passed"] = True
    except Exception as error:
        evidence["failure"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        evidence["formal_listener_pids_after"] = deployed_listener_pids()
        evidence["formal_listener_pid_unchanged"] = evidence["formal_listener_pids_after"] == before
        save_json(root / "acceptance.json", evidence)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--isolated-worker"]:
        worker(Path(sys.argv[2]).resolve(), Path(sys.argv[3]).resolve())
    else:
        main()
