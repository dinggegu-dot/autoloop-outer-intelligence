"""V2 验收目录、契约编译与运行证据。交付文件与验收输入分开保存。"""

import hashlib
import json
import platform
import re
import sys
from copy import deepcopy
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory

from superagent import goal_checks


ENVIRONMENT = "macos-seatbelt-python-v1"
LOCAL_DATA = "local-immutable-artifacts-v1"
MAX_CRITERIA = 12
COORDINATOR_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "schema_version": {"type": "integer", "enum": [1]},
        "input_snapshot_revision": {"type": "string"},
        "action": {"type": "string", "enum": ["CONTINUE", "REPAIR", "REPLAN", "REQUEST_APPROVAL", "BLOCKED"]},
        "reason": {"type": "string"},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
        "plan_change": {"type": "string"},
        "retained_outputs": {"type": "array", "items": {"type": "string"}},
        "scope_change": {"type": "boolean"},
        "approval_required": {"type": "boolean"},
        "remaining_risk": {"type": "string"},
    },
}
COORDINATOR_SCHEMA["required"] = list(COORDINATOR_SCHEMA["properties"])

SCOPED_COORDINATOR = "scoped-v1"
SCOPED_COORDINATOR_TOOLS = (
    "read_failure_evidence", "read_verified_artifacts", "read_plan_versions", "read_remaining_budget",
)
_SCOPED_COORDINATOR_FIELDS = {
    "affected_steps": {"type": "array", "items": {"type": "integer"}},
    "retained_artifacts": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["path", "sha256"],
        "properties": {"path": {"type": "string"}, "sha256": {"type": "string"}},
    }},
    "verification_items": {"type": "array", "items": {"type": "string"}},
}

_RESULT_SCHEMA = {"status": ["PASS", "FAIL", "INCONCLUSIVE", "NOT_RUN", "ERROR"]}
_EVIDENCE_SCHEMA = {
    "required": ["assignment", "run", "artifact_id", "artifact_sha256", "acceptance_revision",
                 "acceptance_digest", "verifier_id", "verifier_version", "fixture_version", "environment",
                 "criterion_id", "status", "detail"]
}
_REGISTRY = {
    "python-json-cli": {
        "artifact_types": ["python", "cli", "data"], "capabilities": ["运行入口", "实际输入输出"],
        "required_inputs": ["script", "args", "files", "expected_result"],
        "supported_environments": [ENVIRONMENT],
        "permission_requirements": ["isolated_read_only_workspace", "network_denied"],
        "runner": "seatbelt-python-json-v1", "resource_limits": {"seconds_per_execution": 3, "executions_per_criterion": 2, "max_contract_criteria": MAX_CRITERIA},
        "trust_status": "TRUSTED",
    },
    "json-artifact": {
        "artifact_types": ["file", "data", "api-contract"], "capabilities": ["JSON 格式与内容精确匹配"],
        "required_inputs": ["path", "expected_result"], "supported_environments": [LOCAL_DATA],
        "permission_requirements": ["immutable_artifacts_only"],
        "runner": "bounded-json-reader-v1", "resource_limits": {"file_bytes": goal_checks.MAX_WORKSPACE_FILE},
        "trust_status": "TRUSTED",
    },
    "html-document": {
        "artifact_types": ["html", "file", "static-web"],
        "capabilities": ["离线解析 UTF-8 HTML 文档，匹配标题、一级标题与 HTML 元素 ID"],
        "required_inputs": ["path", "expected_result"], "supported_environments": [LOCAL_DATA],
        "permission_requirements": ["immutable_artifacts_only"],
        "runner": "bounded-static-html-reader-v1",
        "resource_limits": {"file_bytes": goal_checks.MAX_WORKSPACE_FILE},
        "trust_status": "TRUSTED",
    },
    "blind-projection": {
        "artifact_types": ["file", "data"],
        "capabilities": ["核对匿名成对文本的结构、固定例号顺序和显式来源泄露"],
        "required_inputs": ["path", "expected_result"],
        "supported_environments": [LOCAL_DATA],
        "permission_requirements": ["immutable_artifacts_only"],
        "runner": "bounded-blind-projection-v1",
        "resource_limits": {"file_bytes": goal_checks.MAX_WORKSPACE_FILE},
        "trust_status": "TRUSTED",
    },
    "human-review": {
        "artifact_types": ["any"], "capabilities": ["主观质量或业务判断"],
        "required_inputs": ["description"], "supported_environments": [],
        "permission_requirements": ["authenticated_human_review"],
        "runner": None, "resource_limits": {}, "trust_status": "OWNER_SIGNOFF",
    },
}


def sha256(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def environment_capabilities():
    result = [LOCAL_DATA]
    if sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file():
        result.append(ENVIRONMENT)
    return result


def catalogue():
    available = environment_capabilities()
    return [
        dict(deepcopy(spec), verifier_id=key, version=1, result_schema=deepcopy(_RESULT_SCHEMA),
             evidence_schema=deepcopy(_EVIDENCE_SCHEMA),
             available=bool(set(spec["supported_environments"]).intersection(available)))
        for key, spec in _REGISTRY.items()
    ]


def _text(value, max_length=1000):
    result = goal_checks._text(value, max_length)
    if not result.strip():
        raise ValueError("验收文本不能为空")
    return result


def _list_of_strings(value, *, length=32, max_length=300):
    if not isinstance(value, list) or len(value) > length:
        raise ValueError("列表类型或数量无效")
    result = [_text(item, max_length) for item in value]
    if len(set(result)) != len(result):
        raise ValueError("重复列表元素")
    return result


def _python_spec(criterion_id, spec, expected):
    goal_checks._fields(spec, ("script", "args", "files"))
    check = goal_checks.normalize({
        "version": 1, "stages": [{"id": "goal", "checks": [
            dict(spec, id=criterion_id, expected=expected)
        ]}],
    })["stages"][0]["checks"][0]
    code_suffixes = {".py", ".pyc", ".pyo", ".so", ".dylib", ".sh", ".command", ".pth", ".zip", ".whl"}
    if any(Path(name).suffix.casefold() in code_suffixes for name in check["files"]):
        raise ValueError("合成测试输入只可包含数据文件")
    return {key: check[key] for key in ("script", "args", "files")}, check["expected"]


def _artifact_spec(verifier, spec, expected):
    goal_checks._fields(spec, ("path",))
    path = goal_checks._path(spec["path"])
    if verifier == "json-artifact":
        if not path.endswith(".json") or type(expected) not in (dict, list) or not expected:
            raise ValueError("JSON 产物验收需要 .json 文件和非空预期结构")
        expected = goal_checks._json(expected, [2048, 64 * 1024])
    elif verifier == "blind-projection":
        goal_checks._fields(expected, ("case_ids",))
        ids = _list_of_strings(expected["case_ids"], length=32, max_length=3)
        if not path.endswith(".json") or not ids or any(not re.fullmatch(r"[A-Z][0-9]{2}", key) for key in ids):
            raise ValueError("盲评结构验收须提供 .json 文件和已确定的例号顺序")
        expected = {"case_ids": ids}
    else:
        if not path.endswith((".html", ".htm")):
            raise ValueError("静态 HTML 验收需要 HTML 产物")
        goal_checks._fields(expected, ("title", "headings", "element_ids"))
        headings = _list_of_strings(expected["headings"], length=10)
        element_ids = _list_of_strings(expected["element_ids"], length=20)
        if not headings:
            raise ValueError("静态 HTML 验收必须至少给出一个一级标题")
        expected = {"title": _text(expected["title"],300), "headings": headings, "element_ids": element_ids}
    return {"path": path}, expected


_INTERACTIVE = re.compile(
    r"点击|交互流程|浏览器交互|可交互|页面交互|图形界面|"
    r"HTTP\s*(?:服务|接口|响应)|API\s*(?:服务|端点)|"
    r"\b(?:click|interactive (?:web|ui)|browser interaction|http (?:server|response)|api endpoint)\b", re.I,
)
_EXECUTABLE = re.compile(
    r"可运行|能运行|命令行|实际运行|执行.*(?:程序|脚本)|"
    r"(?:脚本|程序).*(?:从|读取|转换|处理|输出)|"
    r"\b(?:cli|command[- ]line|runnable|executable|script (?:runs?|reads?|processes?|outputs?))\b", re.I,
)


def _distinct_cli_cases(criteria):
    cases = {}
    for row in criteria:
        if (row["required"] and row["verifier_id"] == "python-json-cli"
                and row["environment"] == ENVIRONMENT and not row["human_review_required"]):
            spec = row["test_spec"]
            inputs = sha256([spec["args"],spec["files"]])
            expected = sha256(row["expected_result"])
            cases.setdefault(spec["script"],set()).add((inputs,expected))
    return any(len({inputs for inputs,_ in rows}) >= 2 and len({expected for _,expected in rows}) >= 2
               for rows in cases.values())


def _behavior_gaps(goal, requirements, criteria):
    gaps = []
    # 静态产物可以支撑格式与内容条件。明确要求的运行行为需额外证据。
    for item in [*requirements, {"requirement_id": "目标", "description": goal.get("goal_statement", "")}]:
        text = item.get("description", "")
        key = item["requirement_id"]
        linked = criteria if key == "目标" else [row for row in criteria if key in row["requirement_ids"]]
        if _INTERACTIVE.search(text):
            gaps.append(f"{key} 需要浏览器交互或网络服务的独立运行验收，当前目录未提供")
        elif _EXECUTABLE.search(text) and not _distinct_cli_cases(linked):
            gaps.append(f"{key} 的运行行为须用同一入口的不同输入和不同预期验证")
    return gaps


# 这里只认可与执行器现有硬边界完全一致的表述。自然语言相似性不能授权或证明限制。
_WORKSPACE_WRITE = {
    "只允许写入工单工作区",
    "只在工单工作区内写入",
    "不得写入工单工作区之外",
    "only write within the ticket workspace",
    "do not write outside the ticket workspace",
}


def _explicit_constraints(goal):
    return [item for item in goal.get("constraints", [])
            if item.get("source") in ("USER_EXPLICIT", "USER_CONFIRMED")]


def authorization_constraint_controls(goal):
    return {
        item["requirement_id"]: "write_outside_workspace"
        for item in _explicit_constraints(goal)
        if item["description"].strip().rstrip("。.!").casefold() in _WORKSPACE_WRITE
    }


def validate_constraint_controls(goal, contract, mandate):
    """每项正式限制都须有引擎能实际验证的授权控制。"""
    requirements = {item["requirement_id"] for item in _explicit_constraints(goal)}
    controls = authorization_constraint_controls(goal)
    recorded = contract["coverage_assessment"].get("authorization_controls", {})
    if (set(controls) != requirements or controls != recorded
            or not set(controls.values()) <= set(mandate["forbidden_operations"])):
        raise ValueError("批准的运行授权无法保证目标限制")


def _is_human_criterion(criterion):
    return (criterion.get("verification_type") == "HUMAN_REVIEW"
            and criterion.get("verifier_id") == "human-review"
            and type(criterion.get("verifier_version")) is int and criterion["verifier_version"] == 1
            and criterion.get("human_review_required") is True
            and criterion.get("test_spec") == {} and criterion.get("expected_result") is None
            and criterion.get("environment") is None)


def needs_human_signoff(contract):
    return contract.get("require_signoff") is True or any(
        row.get("required") is True and _is_human_criterion(row) for row in contract["criteria"]
    )


def compile_proposal(goal, proposal):
    """接受受限模型建议，重新计算覆盖，保留任何无法通过程序验证的必要项。"""
    if not isinstance(goal, dict) or not isinstance(proposal, dict):
        raise ValueError("缺少目标或验收方案")
    goal_revision = goal.get("revision", goal.get("goal_revision"))
    if (type(goal_revision) is not int or goal_revision < 1
            or type(proposal.get("goal_revision")) is not int or proposal["goal_revision"] != goal_revision):
        raise ValueError("验收草案引用了旧目标版本")
    if "ticket_id" in proposal and proposal["ticket_id"] != goal.get("ticket_id"):
        raise ValueError("验收草案引用了其他工单")
    items = [item for field in ("required_capabilities", "deliverables", "nonfunctional_requirements")
             for item in goal.get(field, [])]
    constraints = _explicit_constraints(goal)
    constraint_ids = {item["requirement_id"] for item in constraints}
    controls = authorization_constraint_controls(goal)
    requirements = [goal_checks._identifier(item["requirement_id"]) for item in [*items, *constraints]]
    if not requirements or len(set(requirements)) != len(requirements):
        raise ValueError("必要需求编号缺失或重复")
    raw = proposal.get("criteria")
    if not isinstance(raw, list) or len(raw) > MAX_CRITERIA:
        raise ValueError("验收项数量超出上限")
    known = set(requirements)
    environment = environment_capabilities()
    gaps = _list_of_strings(proposal.get("verification_gaps", []))
    human = _list_of_strings(proposal.get("human_review_requirements", []))
    ids, mapped, criteria = set(), set(controls), []
    for criterion in raw:
        if not isinstance(criterion, dict):
            raise ValueError("验收项必须为对象")
        key = goal_checks._identifier(criterion.get("criterion_id"))
        linked = _list_of_strings(criterion.get("requirement_ids"), max_length=64)
        if key in ids or not linked or not set(linked) <= known:
            raise ValueError("验收项重复或引用了不存在的必要需求")
        ids.add(key)
        required = criterion.get("required")
        evidence = criterion.get("evidence_required")
        if isinstance(evidence, list):
            evidence = _list_of_strings(evidence)
        if type(required) is not bool or type(evidence) not in (list, bool):
            raise ValueError("验收项缺少明确的必需和证据标记")
        kind = criterion.get("verification_type")
        human_required = criterion.get("human_review_required")
        if kind not in ("DETERMINISTIC", "MODEL_REVIEW", "HUMAN_REVIEW", "HYBRID") or type(human_required) is not bool:
            raise ValueError("验收方法无法识别")
        verifier = criterion.get("verifier_id")
        version = criterion.get("verifier_version")
        spec = criterion.get("test_spec")
        expected = criterion.get("expected_result")
        if kind == "DETERMINISTIC" and verifier == "python-json-cli" and type(version) is int and version == 1:
            spec, expected = _python_spec(key, spec, expected)
            availability = ENVIRONMENT if ENVIRONMENT in environment else None
        elif (kind == "DETERMINISTIC" and verifier in ("json-artifact", "html-document", "blind-projection")
              and type(version) is int and version == 1):
            spec, expected = _artifact_spec(verifier, spec, expected)
            availability = LOCAL_DATA
        elif kind == "DETERMINISTIC":
            raise ValueError("确定性验收必须使用已登记的方法及版本")
        elif kind == "HUMAN_REVIEW":
            if (verifier != "human-review" or type(version) is not int or version != 1
                    or spec != {} or expected is not None or not human_required):
                raise ValueError("人工验收需要明确的检查条目，不接受脚本或自动预期结果")
            availability = None
        else:
            # 模型不能通过提交脚本或自己的通过判断来消除审查缺口。
            if verifier not in (None, "human-review") or version not in (None, 1) or spec not in (None, {}):
                raise ValueError("人工或模型审查不得附带未登记的执行参数")
            availability = None
            verifier, version, spec, expected = "human-review", 1, {}, None
        if required:
            mapped.update(set(linked) - constraint_ids)
            if kind != "HUMAN_REVIEW" and (not availability or human_required):
                gaps.append(f"{key} 需要独立人工验收或当前环境没有可运行的验证器")
            if not evidence:
                raise ValueError("必需验收项必须要求证据")
        criteria.append({
            "criterion_id": key, "requirement_ids": linked,
            "description": _text(criterion.get("description")), "required": required,
            "verification_type": kind, "verifier_id": verifier, "verifier_version": version,
            "test_spec": spec, "expected_result": expected, "evidence_required": evidence,
            "human_review_required": human_required or kind != "DETERMINISTIC",
            "source_refs": _list_of_strings(criterion.get("source_refs", [])), "environment": availability,
        })
    signoff_rows = [row for row in criteria if row["required"] and _is_human_criterion(row)]
    mapped_human = {key for row in signoff_rows for key in (row["criterion_id"], row["description"])}
    if not set(human) <= mapped_human:
        gaps.append("人工验收要求必须逐项映射到已登记的必需人工验收条目")
    human_descriptions = list(dict.fromkeys(row["description"] for row in signoff_rows))
    gaps.extend(_behavior_gaps(goal, items, criteria))
    unverified_constraints = sorted(constraint_ids - controls.keys())
    if unverified_constraints:
        gaps.append("目标限制缺少可信验证或执行授权控制 " + ", ".join(unverified_constraints))
    uncovered = sorted(known - mapped)
    if uncovered:
        gaps.append("缺少必需验收项的需求 " + ", ".join(uncovered))
    contract = {
        "schema_version": 1, "goal_revision": goal_revision,
        "artifact_types": _list_of_strings(proposal.get("artifact_types", [])),
        "criteria": criteria, "verification_gaps": gaps, "human_review_requirements": human_descriptions,
        "coverage_assessment": {"covered": sorted(mapped), "uncovered": uncovered,
                                "authorization_controls": controls},
        "source_refs": _list_of_strings(proposal.get("source_refs", [])),
        "status": "VERIFICATION_GAP" if gaps else "READY_FOR_APPROVAL",
    }
    if len(goal_checks._encode(contract).encode("utf-8")) > goal_checks.MAX_CONTRACT_BYTES:
        raise ValueError("验收契约超过 256 KiB")
    return contract


class _StaticHtml(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.titles, self.headings, self.element_ids = [], [], []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag in ("title", "h1"):
            self.current = tag, []
        self.element_ids.extend(value for key, value in attrs if key == "id" and value)

    def handle_endtag(self, tag):
        if self.current is not None and tag == self.current[0]:
            result = " ".join("".join(self.current[1]).split())
            (self.titles if tag == "title" else self.headings).append(result)
            self.current = None

    def handle_data(self, data):
        if self.current is not None:
            self.current[1].append(data)


def _html_result(data, expected):
    document = _StaticHtml()
    try:
        document.feed(data.decode("utf-8"))
        document.close()
    except (ValueError, OverflowError, RecursionError):
        return "FAIL", "无法解析 UTF-8 HTML 文档"
    matched = (document.titles == [expected["title"]]
               and all(document.headings.count(heading)==1 for heading in expected["headings"])
               and all(document.element_ids.count(key)==1 for key in expected["element_ids"]))
    return ("PASS", "静态文档标题、一级标题和必要元素 ID 符合预期") if matched else (
        "FAIL", "静态 HTML 文档缺少约定的标题、一级标题或唯一元素 ID"
    )


def _criterion_result(criterion, files, occupied):
    verifier = criterion["verifier_id"]
    spec = criterion["test_spec"]
    if _is_human_criterion(criterion):
        return "INCONCLUSIVE", "等待本人对当前归档产物作出人工验收决定"
    if criterion["human_review_required"] or not criterion["environment"]:
        return "INCONCLUSIVE", "需要独立验收或可信运行环境不可用"
    if criterion["environment"] not in environment_capabilities():
        return "ERROR", "批准的验证环境不可用"
    if verifier == "html-document":
        if spec["path"] not in files:
            return "FAIL", "缺少约定的静态 HTML 产物"
        return _html_result(files[spec["path"]],criterion["expected_result"])
    if verifier == "blind-projection":
        from superagent.goal_blind_review import BlindReviewError, load_json, validate_projection
        if spec["path"] not in files:
            return "FAIL", "缺少冻结的匿名对照材料"
        try:
            validate_projection(load_json(files[spec["path"]]), expected_ids=criterion["expected_result"]["case_ids"])
        except BlindReviewError as error:
            return "FAIL", f"匿名对照结构或来源检查失败（{error.code}，{error.location}）"
        return "PASS", "冻结例号、匿名结构及显式来源标记检查通过"
    if verifier == "json-artifact":
        if spec["path"] not in files:
            return "FAIL", "缺少约定的 JSON 产物"
        try:
            actual = goal_checks._strict_output(files[spec["path"]])
        except (ValueError, OverflowError, RecursionError):
            return "FAIL", "产物必须包含一个有效 UTF-8 JSON 值"
        match = actual == goal_checks._encode(criterion["expected_result"])
        return ("PASS", "JSON 内容符合预期") if match else ("FAIL", "JSON 内容不符合预期")
    if verifier != "python-json-cli":
        return "ERROR", "验收器未注册"
    try:
        policy = goal_checks._policy()
        with TemporaryDirectory(prefix="outer-check-", dir="/private/tmp") as directory:
            check = dict(spec, id=criterion["criterion_id"], expected=criterion["expected_result"])
            goal_checks._verify_check(Path(directory).resolve(), policy, check, files, occupied)
    except (OSError, RuntimeError):
        return "ERROR", "本地验证环境准备失败"
    except ValueError as exc:
        detail = str(exc)
        if detail.startswith("Seatbelt"):
            return "ERROR", "隔离能力探测失败"
        return "FAIL", detail[:240]
    return "PASS", "两次隔离运行产生与预期一致的实际 JSON 输出"


def _binding(criterion, contract, assignment, checkpoint):
    return {
        "assignment": assignment["id"], "run": assignment["run"],
        "artifact_id": f"sha256:{checkpoint['digest']}", "artifact_sha256": checkpoint["digest"],
        "goal_revision": assignment["outer_binding"]["goal_revision"],
        "acceptance_revision": assignment["outer_binding"]["acceptance_revision"],
        "acceptance_digest": sha256(contract),
        "mandate_revision": assignment["outer_binding"]["mandate_revision"],
        "plan_revision": assignment["version"], "criterion_id": criterion["criterion_id"],
        "verifier_id": criterion["verifier_id"], "verifier_version": criterion["verifier_version"],
        "fixture_version": sha256([criterion["test_spec"], criterion["expected_result"]]),
        "environment": {"profile": criterion["environment"], "platform": sys.platform,
                        "os_release": platform.release()},
    }


def selected_criteria(contract, plan, step, *, additional=()):
    if step + 1 == len(plan["steps"]):
        return contract["criteria"]
    keys = set(plan["steps"][step]["acceptance"]).union(additional)
    return [criterion for criterion in contract["criteria"] if criterion["criterion_id"] in keys]


def capture(workspace):
    """为归档、版本登记及复验提供同一受限的只读快照。"""
    files, occupied = goal_checks._snapshot(workspace)
    files = {key: value for key, value in files.items() if Path(key).name != "AGENTS.md"}
    occupied = {key: value for key, value in occupied.items() if Path(key).name != "AGENTS.md"}
    observed = {key: hashlib.sha256(value).hexdigest() for key, value in sorted(files.items())}
    checkpoint = {"digest": sha256(list(observed.items())), "files": observed, "artifacts": list(observed)}
    return checkpoint, files, occupied


def verify(contract, assignment, checkpoint, files, occupied):
    """可信 Worker 对单一工作区快照运行验证器，候选产物的 stdout 不进入证据。"""
    rows = []
    for criterion in selected_criteria(contract, assignment["plan"], assignment["step"],
                                       additional=assignment.get("additional_review_items", ())):
        status, detail = _criterion_result(criterion, files, occupied)
        row = dict(_binding(criterion, contract, assignment, checkpoint), status=status, detail=detail)
        row["environment"]["python"] = platform.python_version()
        rows.append(dict(row, id=criterion["criterion_id"], passed=status == "PASS",
                         evidence_ref=f"sha256:{sha256(row)}"))
    return rows


def validate_results(contract, plan, assignment, checkpoint, rows):
    criteria = selected_criteria(contract, plan, assignment["step"],
                                 additional=assignment.get("additional_review_items", ()))
    if type(rows) is not list or len(rows) != len(criteria) or not criteria:
        raise ValueError("缺少与阶段绑定的逐项验收结果")
    for criterion, row in zip(criteria, rows):
        binding = _binding(criterion, contract, assignment, checkpoint)
        environment = row.get("environment") if isinstance(row, dict) else None
        worker_python = environment.get("python") if isinstance(environment, dict) else None
        if not isinstance(worker_python,str) or not re.fullmatch(r"3\.[0-9]{1,3}\.[0-9]{1,3}(?:(?:a|b|rc)[0-9]{1,3})?", worker_python):
            raise ValueError("验收结果缺少有效的 Worker Python 版本")
        # Worker 解释器由可信回执记录。协调器仍独立核对批准环境和同一宿主机的 OS。
        binding["environment"]["python"] = worker_python
        if any(row.get(key) != value for key, value in binding.items()):
            raise ValueError("验收结果绑定的产物、版本或环境不匹配")
        status = row.get("status")
        valid_status = status in _RESULT_SCHEMA["status"]
        signed_body = {key: row.get(key) for key in (*binding, "status", "detail")}
        if (not valid_status or row.get("passed") is not (status == "PASS")
                or row.get("id") != criterion["criterion_id"]
                or row.get("evidence_ref") != f"sha256:{sha256(signed_body)}"):
            raise ValueError("验收结果缺少有效证据")
        human = _is_human_criterion(criterion)
        if human and status != "INCONCLUSIVE":
            raise ValueError("自动核验不得代替人工验收决定")
        if status == "PASS" and (criterion["human_review_required"] or not criterion["environment"]):
            raise ValueError("独立验收未确认")
    required = [(criterion,row) for criterion, row in zip(criteria, rows) if criterion["required"]]
    return bool(required) and all(row["status"] == "PASS" or _is_human_criterion(criterion)
                                  for criterion, row in required)


def coordination_schema(context):
    schema = deepcopy(COORDINATOR_SCHEMA)
    fields = schema["properties"]
    if context.get("coordinator_profile") == SCOPED_COORDINATOR:
        fields.update(deepcopy(_SCOPED_COORDINATOR_FIELDS))
        schema["required"].extend(_SCOPED_COORDINATOR_FIELDS)
        fields["schema_version"]["enum"] = [2]
        fields["affected_steps"]["items"]["enum"] = context.get("replan_affected_steps", context["affected_steps"])
        checks = context.get("replan_verification_items", context["verification_items"])
        if checks:
            fields["verification_items"]["items"]["enum"] = checks
        else:
            fields["verification_items"]["maxItems"] = 0
        if context["retained_outputs"]:
            fields["retained_artifacts"]["items"]["properties"]["path"]["enum"] = context["retained_outputs"]
        else:
            fields["retained_artifacts"]["maxItems"] = 0
    fields["input_snapshot_revision"]["enum"] = [context["input_snapshot_revision"]]
    fields["action"]["enum"] = list(context["available_actions"])
    fields["evidence_refs"]["items"]["enum"] = list(context["evidence_refs"])
    fields["evidence_refs"]["minItems"] = 1
    retained = context["retained_outputs"]
    if retained:
        fields["retained_outputs"]["items"]["enum"] = list(retained)
    else:
        fields["retained_outputs"]["maxItems"] = 0
    return schema


def validate_coordination(result, context):
    scoped = context.get("coordinator_profile") == SCOPED_COORDINATOR
    required = set(COORDINATOR_SCHEMA["required"])
    if scoped:
        required.update(_SCOPED_COORDINATOR_FIELDS)
    if not isinstance(result, dict) or set(result) != required:
        raise ValueError("协调建议字段无效")
    action = result["action"]
    if (type(result["schema_version"]) is not int or result["schema_version"] != (2 if scoped else 1)
            or result["input_snapshot_revision"] != context["input_snapshot_revision"]
            or action not in context["available_actions"]):
        raise ValueError("协调建议版本或动作已失效")
    refs = _list_of_strings(result["evidence_refs"])
    if not refs or not set(refs) <= set(context["evidence_refs"]):
        raise ValueError("协调建议必须引用实际失败证据")
    retained = _list_of_strings(result["retained_outputs"], length=128)
    if not set(retained) <= set(context["retained_outputs"]):
        raise ValueError("协调建议引用了不存在的成果")
    scope, approval = result["scope_change"], result["approval_required"]
    if type(scope) is not bool or type(approval) is not bool:
        raise ValueError("协调建议缺少授权判断")
    if (scope or approval) and action != "REQUEST_APPROVAL":
        raise ValueError("协调不能扩大正式授权")
    _text(result["reason"])
    _text(result["remaining_risk"])
    if action == "REPLAN" or result["plan_change"]:
        _text(result["plan_change"])
    if scoped:
        _validate_scoped_coordination(result, context, retained)
    return deepcopy(result)


def _validate_scoped_coordination(result, context, retained):
    action = result["action"]
    available_steps = context["affected_steps"]
    available_checks = context["verification_items"]
    if action == "REPLAN":
        available_steps = context.get("replan_affected_steps", available_steps)
        available_checks = context.get("replan_verification_items", available_checks)
    steps = result["affected_steps"]
    if (type(steps) is not list or len(steps) > len(available_steps)
            or any(type(step) is not int or step not in available_steps for step in steps)
            or len(set(steps)) != len(steps)):
        raise ValueError("协调建议引用了未登记的阶段")
    checks = _list_of_strings(result["verification_items"])
    if not set(checks) <= set(available_checks):
        raise ValueError("协调建议引用了未批准的验收项")
    if action in ("CONTINUE", "REPAIR", "REPLAN"):
        if set(result["evidence_refs"]) != set(context["evidence_refs"]):
            raise ValueError("继续派工必须覆盖本轮失败证据")
        if set(checks) != set(available_checks) or set(steps) != set(available_steps):
            raise ValueError("继续派工必须覆盖受影响阶段和下一轮验收")
        if action in ("CONTINUE", "REPAIR") and set(retained) != set(context["artifact_versions"]):
            raise ValueError("继续或返工必须保留系统确认的全部文件版本")
    elif action == "REQUEST_APPROVAL":
        if result["approval_required"] is not True:
            raise ValueError("建议申请授权必须标明需本人批准")
        _text(result["plan_change"])
    artifacts = result["retained_artifacts"]
    if type(artifacts) is not list or len(artifacts) != len(retained):
        raise ValueError("保留成果必须逐项绑定文件版本")
    paths = []
    for item in artifacts:
        if (not isinstance(item, dict) or set(item) != {"path", "sha256"}
                or not isinstance(item["path"], str) or item["path"] not in retained
                or item["sha256"] != context["artifact_versions"].get(item["path"])):
            raise ValueError("保留成果文件版本不匹配")
        paths.append(item["path"])
    if set(paths) != set(retained):
        raise ValueError("保留成果必须逐项绑定文件版本")
