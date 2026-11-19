"""隔离试点的持久协调器。使用真正的 Autoloop 事件、Worker 和 Run。"""
import argparse
import json
import os
import sqlite3
import time
from collections import Counter
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

CASE = Path(__file__).with_name("case.json")
WORK = {
    "aggregate": ("按规则自行计算各发生日期的件数。用 counts 列出 date 与 total。", ()),
    "reconcile": ("独立核账，返回有效行数 valid、有效件数 total、剔除行号 rejected。", ()),
    "review": ("独立逐行复算原始数据，再检查两个报告。只有所有口径和数值正确才 passed。"
               "另一次输出 independently recomputed counts、valid、total、rejected，findings 写出剔除原因。", ("aggregate", "reconcile")),
}
MODEL_TIMEOUT = 150


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return sha256(encode(value).encode()).hexdigest()


def plan():
    return {
        "case": json.loads(CASE.read_text()), "model": "gpt-6.2-sol", "timeout": MODEL_TIMEOUT,
        "tools": ["Codex read-only"], "destinations": ["Codex synthetic input only", "local isolated ledger"],
        "assignments": {name: {"objective": goal, "requires": list(deps)} for name, (goal, deps) in WORK.items()},
    }


@contextmanager
def ledger(runtime):
    db = sqlite3.connect(runtime / "ticket.sqlite", timeout=15)
    db.row_factory = sqlite3.Row
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            yield db
    finally:
        db.close()


def load_plan(db):
    row = db.execute("SELECT * FROM mandate").fetchone()
    frozen = json.loads(row["plan"])
    if digest(frozen) != row["digest"]:
        raise RuntimeError("授权方案摘要不匹配")
    return frozen, row["digest"]


def prepare(runtime, approved, sequential=False):
    frozen = plan()
    signature = digest(frozen)
    if signature != approved:
        raise ValueError("请用 plan 显示的摘要批准合成验收方案")
    with ledger(runtime) as db:
        db.execute("CREATE TABLE IF NOT EXISTS mandate (id INTEGER PRIMARY KEY CHECK(id=1), digest TEXT, plan TEXT)")
        db.execute("""CREATE TABLE IF NOT EXISTS assignment (
                   id TEXT PRIMARY KEY, capability TEXT NOT NULL, attempt INTEGER NOT NULL,
                   state TEXT NOT NULL, event INTEGER, run INTEGER, result TEXT, evidence TEXT, error TEXT,
                   UNIQUE(capability,attempt))""")
        db.execute("INSERT OR IGNORE INTO mandate VALUES (1,?,?)", (signature, encode(frozen)))
        db.execute("CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), sequential INTEGER NOT NULL)")
        db.execute("INSERT OR IGNORE INTO settings VALUES (1,?)",(int(sequential),))
        if db.execute("SELECT sequential FROM settings WHERE id=1").fetchone()[0] != int(sequential):
            raise RuntimeError("同一工单无法切换执行模式")
        if load_plan(db)[1] != signature:
            raise RuntimeError("已有不同版本的工单，请保留旧目录并新建试点目录")
        for cap in frozen["assignments"]:
            db.execute("INSERT OR IGNORE INTO assignment VALUES (?,?,1,'pending',NULL,NULL,NULL,NULL,NULL)",
                       (assignment_id(signature, cap, 1), cap))


def assignment_id(signature, cap, attempt):
    return f"team-{signature[:16]}-{cap}-{attempt}"


def latest(db):
    return {row["capability"]: dict(row) for row in db.execute(
        "SELECT * FROM assignment ORDER BY attempt")}


def expected(case):
    seen, totals, rejected, valid = set(), Counter(), [], 0
    for row in case["rows"]:
        duplicate = row["id"] in seen
        seen.add(row["id"])
        if duplicate or row["status"] != "paid":
            rejected.append(row["row"])
        else:
            valid += 1
            totals[row["occurrence"]] += row["units"]
    return {
        "aggregate": {"counts": [{"date": date, "total": total} for date, total in sorted(totals.items())]},
        "reconcile": {"valid": valid, "total": sum(totals.values()), "rejected": rejected},
    }


class DispatchBindingPending(Exception):
    pass


def claim(runtime, capability, context, run):
    events = context.get("events", [])
    if len(events) != 1:
        raise ValueError("只接受一次派工事件")
    event = events[0]
    key = event["id"]
    if (event.get("source"), event.get("type"), event.get("subject")) != ("team", "team."+capability, key):
        raise ValueError("派工来源、事项或能力不匹配")
    with ledger(runtime) as db:
        frozen, signature = load_plan(db)
        row = latest(db).get(capability)
        if not row or row["id"] != key or key != assignment_id(signature, capability, row["attempt"]):
            raise ValueError("当前授权下没有这个派工")
        if row["run"] is None and row["state"] == "dispatching":
            raise DispatchBindingPending("等待协调器确认 Autoloop Run 编号")
        if row["run"] != run:
            raise ValueError("领取的 Run 与可信派工绑定不符")
        if row["state"] in ("succeeded", "failed"):
            return None
        spec = frozen["assignments"][capability]
        if row["state"] != "dispatching":
            raise ValueError("派工已领取或不再有效")
        results = latest(db)
        if any(results[dep]["state"] != "succeeded" for dep in spec["requires"]):
            raise ValueError("依赖尚未通过，拒绝领取")
        inputs = dict(frozen["case"])
        if spec["requires"]:
            inputs["reports"] = {dep: json.loads(results[dep]["result"]) for dep in spec["requires"]}
            inputs["reports_digest"] = digest(inputs["reports"])
        db.execute("UPDATE assignment SET state='running' WHERE id=?", (key,))
        return spec["objective"], inputs, frozen["timeout"]


def worker(runtime, capability):
    from codex_worker import invoke
    context = json.loads(Path(os.environ["AGENT_AUTOLOOP_CONTEXT"]).read_text())
    run = int(os.environ["AGENT_AUTOLOOP_RUN"])
    until = time.monotonic()+20
    while True:
        try:
            claimed = claim(runtime, capability, context, run)
            break
        except DispatchBindingPending:
            if time.monotonic() >= until:
                raise
            time.sleep(.2)
    key = context["events"][0]["id"]
    if claimed:
        objective, inputs, timeout = claimed
        output = runtime / "attempts" / key
        result, evidence, error = None, None, None
        try:
            output.mkdir(parents=True, mode=0o700)
            result, evidence = invoke(capability, objective, inputs, output, timeout)
            if capability == "review":
                evidence["reports_digest"] = inputs["reports_digest"]
                required=expected(inputs)
                recomputed={key: result[key] for key in ("total","valid","rejected")}
                if (result.get("passed") is not True or not result.get("findings")
                        or any(not finding.strip() for finding in result["findings"])
                        or encode(recomputed)!=encode(required["reconcile"])
                        or encode({"counts":result["counts"]})!=encode(required["aggregate"])):
                    error = "独立审核未通过"
            elif encode(result) != encode(expected(inputs)[capability]):
                error = "确定性账目检查失败"
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:300]}"
        with ledger(runtime) as db:
            changed = db.execute("""UPDATE assignment SET state=?,result=?,evidence=?,error=?
                                    WHERE id=? AND run=? AND state='running'""",
                                 ("failed" if error else "succeeded", encode(result), encode(evidence), error,
                                  key, run)).rowcount
            if changed != 1:
                raise RuntimeError("派工已失效，回执未提交")
    # Autoloop 运输成功表示回执可核对；业务成功须另读持久记录。
    print("```agent-autoloop\n"+encode({"outcome": "done", "summary": f"派工回执 {key}"})+"\n```")


class Autoloop:
    def __init__(self, runtime):
        self.connection = json.loads((runtime / "hub.json").read_text())
        from urllib.parse import urlsplit
        url = urlsplit(self.connection["url"])
        if (url.scheme, url.hostname) != ("http", "127.0.0.1") or url.username or url.path:
            raise ValueError("验收仅允许连接临时回环 Autoloop")
        self.http = build_opener(ProxyHandler({}))

    def request(self, path, event=None):
        key = self.connection["source_key"]
        request = Request(self.connection["url"]+path, encode(event).encode() if event else None,
                          {"Authorization": "Bearer "+key, "Content-Type": "application/json"})
        with self.http.open(request, timeout=5) as response:
            return json.load(response)

    def submit(self, row):
        cap, key = row["capability"], row["id"]
        receipt = self.request("/v1/events", {"id": key, "type": "team."+cap, "subject": key})
        if receipt.get("routed") != ["team-"+cap]:
            raise RuntimeError("临时 Autoloop 未按唯一能力路由")
        return receipt["event"]

    def outcome(self, event):
        runs = self.request(f"/v1/events/{event}")["runs"]
        if len(runs) > 1:
            raise RuntimeError("同一派工出现多个运输轮次")
        return runs[0] if runs else {}


def schedule(runtime, hub, sequential=False):
    with ledger(runtime) as db:
        frozen, _ = load_plan(db)
        rows = latest(db)
        for cap, row in rows.items():
            if row["state"] == "pending" and all(rows[d]["state"] == "succeeded" for d in frozen["assignments"][cap]["requires"]):
                if sequential and cap == "reconcile" and rows["aggregate"]["state"] != "succeeded":
                    continue
                db.execute("UPDATE assignment SET state='dispatching' WHERE id=?", (row["id"],))
                row["state"] = "dispatching"
    for row in rows.values():
        if row["state"] != "pending" and row["event"] is None:
            event = hub.submit(row)
            with ledger(runtime) as db:
                db.execute("UPDATE assignment SET event=? WHERE id=?", (event, row["id"]))
    with ledger(runtime) as db:
        rows = latest(db)
    for row in rows.values():
        if row["event"] is not None:
            outcome = hub.outcome(row["event"])
            if row["run"] is not None and outcome.get("run") not in (None, row["run"]):
                raise RuntimeError("Autoloop Run 与派工业务回执不一致")
            transport = outcome.get("state")
            with ledger(runtime) as db:
                if outcome.get("run") is not None:
                    db.execute("UPDATE assignment SET run=? WHERE id=? AND run IS NULL",(outcome["run"],row["id"]))
                if transport in ("nothing", "cancelled", "done"):
                    db.execute("""UPDATE assignment SET state='failed',error='运输已结束但业务回执缺失'
                                  WHERE id=? AND state IN ('dispatching','running')""", (row["id"],))
                rows[row["capability"]] = dict(db.execute("SELECT * FROM assignment WHERE id=?",(row["id"],)).fetchone())
            rows[row["capability"]]["transport"] = transport
    return rows



def retry_failed(runtime, hub, cap, approved):
    # 只为此固定合成工单开放人工重试。先向中台确认旧轮退出。
    with ledger(runtime) as db:
        _, signature = load_plan(db)
        row = latest(db)[cap]
    if signature != approved or row["state"] != "failed" or row["attempt"] >= 2:
        raise ValueError("重试须匹配原授权，且每包仅能追加一次已失败尝试")
    if row["event"] is None or hub.outcome(row["event"]).get("state") not in ("done", "nothing", "cancelled"):
        raise RuntimeError("旧轮状态待核对，保留工单和尝试，不重复派发")
    with ledger(runtime) as db:
        if latest(db)[cap] != row:
            raise RuntimeError("尝试已变化，请重新核对")
        next_attempt = row["attempt"]+1
        db.execute("INSERT INTO assignment (id,capability,attempt,state) VALUES (?,?,?,'pending')",
                   (assignment_id(signature, cap, next_attempt), cap, next_attempt))

def run_ticket(runtime, approved, limit, sequential):
    prepare(runtime, approved, sequential)
    hub = Autoloop(runtime)
    started = time.monotonic()
    while time.monotonic()-started < limit:
        rows = schedule(runtime, hub, sequential)
        if any(r["state"] == "failed" for r in rows.values()):
            raise RuntimeError("工单待处理："+encode({cap: r["error"] for cap,r in rows.items() if r["error"]}))
        if all(r["state"] == "succeeded" and r.get("transport") == "done" for r in rows.values()):
            finish(runtime, rows, started, sequential)
            return
        time.sleep(.5)
    raise TimeoutError("等待时限已到。保留工单运行记录，重新运行同一命令以核对进度。")


def finish(runtime, rows, started, sequential):
    evidence = {cap: json.loads(r["evidence"]) for cap,r in rows.items()}
    results = {cap: json.loads(r["result"]) for cap,r in rows.items()}
    agg, recon, review = (evidence[cap] for cap in WORK)
    if len({e["session"] for e in evidence.values()}) != 3:
        raise RuntimeError("缺少三个独立 Codex 会话")
    overlap = min(agg["ended_ns"],recon["ended_ns"])-max(agg["started_ns"],recon["started_ns"])
    if review["started_ns"] < max(agg["ended_ns"],recon["ended_ns"]):
        raise RuntimeError("审核在合流前进入")
    if review["reports_digest"] != digest({cap: results[cap] for cap in ("aggregate","reconcile")}):
        raise RuntimeError("审核对应的报告摘要不匹配")
    if sequential and recon["started_ns"] < agg["ended_ns"]:
        raise RuntimeError("串行对照的两包出现重叠")
    if not sequential and overlap <= 0:
        raise RuntimeError("没有实际并行时间，不能通过并行验收")
    proof = {
        "status":"completed", "mode":"sequential" if sequential else "parallel",
        "mandate_digest":digest(plan()), "wall_seconds_this_run":round(time.monotonic()-started,3),
        "worker_overlap_seconds":round(overlap/1e9,3),
        "assignments": {cap: {"event": row["event"],"run":row["run"],"result":results[cap],
                              "evidence":evidence[cap]} for cap,row in rows.items()},
    }
    target = runtime/"evidence.json"
    if target.exists():
        saved = json.loads(target.read_text())
        if any(saved[key] != proof[key] for key in ("mandate_digest", "mode", "assignments")):
            raise RuntimeError("已有交付证据与当前派工不一致")
        print(f"已核对原交付，不重复调用 Codex。证据见 {target}", flush=True)
        return
    staging = runtime/"evidence.new"
    staging.write_text(json.dumps(proof,ensure_ascii=False,indent=2)+"\n")
    os.replace(staging,target)
    print(f"验收通过，证据见 {target}",flush=True)


def main():
    parser = argparse.ArgumentParser()
    modes = parser.add_subparsers(dest="command", required=True)
    modes.add_parser("plan")
    for mode in ("worker","run","retry"):
        cmd = modes.add_parser(mode)
        cmd.add_argument("--runtime", type=Path,required=True)
        if mode == "worker":
            cmd.add_argument("--capability",choices=WORK,required=True)
        else:
            cmd.add_argument("--approve-digest",required=True)
            if mode == "retry":
                cmd.add_argument("--capability",choices=WORK,required=True)
            else:
                cmd.add_argument("--limit",type=int,default=400)
                cmd.add_argument("--sequential",action="store_true")
    args = parser.parse_args()
    if args.command == "plan":
        frozen = plan()
        print(json.dumps({"digest":digest(frozen),"plan":frozen},ensure_ascii=False,indent=2))
    elif args.command == "worker":
        worker(args.runtime, args.capability)
    elif args.command == "retry":
        retry_failed(args.runtime, Autoloop(args.runtime), args.capability, args.approve_digest)
    else:
        run_ticket(args.runtime,args.approve_digest,args.limit,args.sequential)


if __name__ == "__main__":
    main()
