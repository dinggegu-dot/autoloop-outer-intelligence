"""8765 的首单灰度入口。运行时只转发固定合成任务和版本绑定的控制请求。"""
import hmac
from http.client import HTTPConnection, HTTPException
from http.cookies import SimpleCookie
import json
import os
import re
from urllib.parse import urlsplit
from uuid import UUID

from formal_outer_canary_wire import client_ticket, response_proof


SYNTHETIC_ORDER = {
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
    ),
    "request_id": "11111111-2222-4333-8444-101010101021",
}
FIXTURE = {"workflow": "outer", "fixture": "filename-index-v2"}
TEAM_FIXTURE = {"workflow": "outer", "fixture": "goal-team-brief-v1"}
TEAM_ORDER = {
    "workflow": "outer",
    "name": "合成活动简报 · 团队审稿",
    "goal": (
        "制作 stable-outline.html，网页标题为 Plan Sheet，含一级标题 Source Outline 和元素 brief-outline。"
        "制作 stable-brief.html，网页标题为 Event Brief，含一级标题 Launch Brief 和元素 release-notice。"
        "所有活动和材料均为合成示例。"
    ),
    "acceptance": (
        "验收一：stable-outline.html 的网页标题是 Plan Sheet，一级标题 Source Outline 出现一次，ID 为 brief-outline 的元素出现一次。"
        "验收二：stable-brief.html 的网页标题是 Event Brief，一级标题 Launch Brief 出现一次，ID 为 release-notice 的元素出现一次。"
    ),
    "request_id": "11111111-2222-4333-8444-101010101023",
    "team_review_mode": "review_and_decide",
}
PREFIX = "/api/outer-canary/"
TICKET = r"wb-[a-f0-9]{32}"
ASSETS = {
    "/workbench": "index.html",
    "/outer-canary/app.js": "app.js",
    "/outer-canary/outer.js": "outer.js",
    "/outer-canary/style.css": "style.css",
    "/outer-canary/goal_team.js": "goal_team.js",
    "/outer-canary/goal_team.css": "goal_team.css",
    "/outer-canary/task_presentation.js": "task_presentation.js",
}
MIMES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}
CONTROL_FIELDS = {
    "get": {"ticket_id"}, "calls": {"ticket_id"}, "call": {"ticket_id", "call_id"}, "event": {"event_id"},
    "approve": {"ticket_id", "revision", "goal_revision", "acceptance_revision", "request_id"},
    "pause": {"ticket_id", "revision", "request_id"}, "resume": {"ticket_id", "revision", "request_id"},
    "stop": {"ticket_id", "revision", "request_id"},
}
MAX_RESPONSE = 16 * 1024 * 1024


def canonical_uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


def valid_command(command, body):
    checks = {
        "ticket_id": lambda v: isinstance(v, str) and re.fullmatch(TICKET, v),
        "event_id": lambda v: isinstance(v, str) and re.fullmatch(r"evt-[a-f0-9]{32}", v),
        "call_id": lambda v: isinstance(v, str) and re.fullmatch(r"(?:call-)?[a-f0-9]{12,64}", v),
        "request_id": canonical_uuid,
        "revision": lambda v: type(v) is int and v > 0,
        "goal_revision": lambda v: type(v) is int and v > 0,
        "acceptance_revision": lambda v: type(v) is int and v > 0,
        "question_revision": lambda v: type(v) is int and v > 0,
        "question_id": lambda v: isinstance(v, str) and re.fullmatch(r"[a-f0-9]{32}", v),
        "candidate": lambda v: isinstance(v, str) and re.fullmatch(r"[a-f0-9]{32}", v),
        "candidate_digest": lambda v: isinstance(v, str) and re.fullmatch(r"[a-f0-9]{64}", v),
        "artifact_sha256": lambda v: isinstance(v, str) and re.fullmatch(r"[a-f0-9]{64}", v),
        "require_signoff": lambda v: type(v) is bool,
        "answer": lambda v: (
            isinstance(v, dict) and v.keys() == {"option_id", "text"} and v["text"] == ""
            and isinstance(v["option_id"], str) and re.fullmatch(r"[a-z0-9_-]{1,64}", v["option_id"])
        ),
    }
    binding = CONTROL_FIELDS["approve"]
    fields = CONTROL_FIELDS.get(command)
    if command == "approve" and "require_signoff" in body:
        fields = binding | {"require_signoff"}
    elif command in {"pause", "resume", "stop"} and "goal_revision" in body:
        fields = binding
    elif command == "answer_preference":
        fields = binding | {"question_id", "question_revision", "candidate_digest", "answer"}
    elif command == "accept_artifact":
        fields = binding | {"candidate", "artifact_sha256"}
    if not fields or body.keys() != fields:
        return False
    return all(checks[key](value) for key, value in body.items())


class OuterCanary:
    def __init__(self, profile, ui):
        key_path = profile.parent / "gateway.key"
        for path in (profile.parent, profile, key_path):
            if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
                raise ValueError("灰度配置必须由本人私有")
        data = json.loads(profile.read_text())
        self.port = data["board_port"]
        self.secret = key_path.read_text().strip()
        if type(self.port) is not int or not 1024 <= self.port <= 65535 or self.port == 8765:
            raise ValueError("灰度服务端口无效")
        if not re.fullmatch(r"[a-f0-9]{64}", self.secret):
            raise ValueError("灰度本机连接凭证无效")
        self.ui = ui
        self.origin = f"http://127.0.0.1:{self.port}"

    def _request(self, method, route, headers, body=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        ticket = client_ticket(self.secret, method, route)
        try:
            connection.request(method, route, body, {**headers, "X-Odyssey-Canary-Gateway": ticket})
            response = connection.getresponse()
            payload = response.read(MAX_RESPONSE + 1)
            if len(payload) > MAX_RESPONSE or response.status not in (200, 201, 400, 403, 404, 409, 422, 503):
                raise ValueError("灰度连接返回无效状态")
            proof = response_proof(self.secret, ticket.split(".")[0], response.status, payload)
            if not hmac.compare_digest(response.headers.get("X-Odyssey-Canary-Proof", ""), proof):
                raise ValueError("灰度连接未能证明本机身份")
            return response.status, response.headers, payload
        finally:
            connection.close()

    def forward(self, handler, route, body=None):
        headers = {}
        try:
            status, session_headers, response = self._request("GET", "/api/session", headers)
            if status != 200:
                raise ValueError("灰度会话不可用")
            name = f"odyssey_session_{self.port}"
            cookie = SimpleCookie(session_headers["Set-Cookie"])[name].value
            headers["Cookie"] = f"{name}={cookie}"
            if body is not None:
                headers.update({"Content-Type": "application/json", "X-Odyssey-CSRF": json.loads(response)["csrf"],
                                "Origin": self.origin})
            status, result_headers, payload = self._request(
                "POST" if body is not None else "GET", route, headers,
                None if body is None else json.dumps(body, ensure_ascii=False).encode())
            mime = result_headers.get("Content-Type", "").split(";", 1)[0]
            download = re.fullmatch(f"/api/tasks/{TICKET}/artifact", urlsplit(route).path)
            allowed = "application/octet-stream" if download and status == 200 else "application/json"
            if mime != allowed:
                raise ValueError("灰度连接返回无效内容")
            disposition = result_headers.get("Content-Disposition")
        except (OSError, HTTPException, ValueError, KeyError, TypeError):
            return handler.json_response({"error": "智能工单试点暂不可用，请在工单簿核对后重试"}, 503)
        result = [("Content-Disposition", disposition)] if allowed == "application/octet-stream" and disposition else []
        return handler.respond(payload, result_headers["Content-Type"], status, result)

    def post(self, handler, route, body):
        if route == "/api/tasks":
            if body == FIXTURE:
                return self.forward(handler, "/api/tasks", SYNTHETIC_ORDER)
            if body == TEAM_FIXTURE:
                return self.forward(handler, "/api/tasks", TEAM_ORDER)
            return handler.json_response({"error": "灰度入口仅接受内置合成工单"}, 409)
        command = route.removeprefix(PREFIX)
        if not valid_command(command, body):
            return handler.json_response({"error": "灰度工单操作或者版本格式无效"}, 400)
        return self.forward(handler, "/api/outer/" + command, body)

    def get(self, handler, route):
        if route in ASSETS:
            path = self.ui / ASSETS[route]
            return handler.respond(path.read_bytes(), MIMES[path.suffix] + "; charset=utf-8")
        if not handler.session():
            return handler.json_response({"error": "会话已过期，请刷新"}, 403)
        query = urlsplit(handler.path).query
        if len(query) > 4096:
            return handler.json_response({"error": "灰度工单查询参数过长"}, 400)
        suffix = "?" + query if query else ""
        endpoint = route.removeprefix(PREFIX)
        if endpoint == "drafts":
            return self.forward(handler, "/api/outer")
        if endpoint == "tasks" or re.fullmatch(f"tasks/{TICKET}/(?:artifact|draft-preview)", endpoint):
            return self.forward(handler, "/api/" + endpoint + suffix)
        return handler.json_response({"error": "灰度工单接口不存在"}, 404)
