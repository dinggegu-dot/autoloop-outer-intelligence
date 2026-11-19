#!/bin/bash
set -euo pipefail
umask 077

fail() {
  printf '%s\n' "$*" >&2
  exit 1
}

resume=false
capacity=1
team=false
receipt_loss=false
case ${1:-} in
  --resume) resume=true; shift ;;
  --team) team=true; capacity=4; shift ;;
  --team-capacity)
    [[ ${2:-} =~ ^[1-4]$ ]] || fail "隔离团队容量须为 1 到 4"
    team=true; capacity=$2; shift 2 ;;
  --receipt-loss-test) receipt_loss=true; shift ;;
esac
if [[ $# -lt 1 || $1 != /* || ! -d $1 || -L $1 ]]; then
  fail "用法：$0 [--team | --resume] /绝对/试点目录 [端口 [-- 命令 参数...]]"
fi
runtime=$(cd -- "$1" && pwd -P)
port=${2:-43350}
[[ "$port" =~ ^[0-9]{1,5}$ ]] && (( 10#$port >= 1 && 10#$port <= 65535 )) ||
  fail "端口必须在 1 到 65535 之间"
port=$((10#$port))
if (( $# >= 2 )); then shift 2; else shift; fi
if [[ ${1:-} == -- ]]; then
  shift
  (( $# > 0 )) || fail "-- 后需要提供命令"
fi
[[ "$runtime" == /private/tmp/* ]] || fail "runtime 必须位于 /private/tmp 下"
[[ -O "$runtime" ]] || fail "runtime 必须由当前用户拥有"
if $resume; then
  [[ "$runtime" == /private/tmp/* && -f "$runtime/.goal-pilot" && ! -L "$runtime/.goal-pilot" &&
     $(<"$runtime/.goal-pilot") == "$port" && -f "$runtime/hub/config.json" ]] || fail "恢复仅允许原端口对应的临时试点目录"
else
  [[ -z $(/bin/ls -A "$runtime") ]] || fail "新运行的 runtime 必须为空"
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
worker_script="$script_dir/goal_runtime.py"
if $receipt_loss; then worker_script="$script_dir/../验收样例/目标驱动CSV/回执中断注入.py"; fi
[[ -f "$worker_script" && -r "$worker_script" ]] || fail "找不到可读的 $worker_script"
python=$(command -v python3) || fail "找不到 python3"
[[ "$python" == /* && -x "$python" ]] || fail "python3 必须是 PATH 中的可执行文件"
install="$HOME/.agent-autoloop"
node="$install/runtime/node-v24.22.2/bin/node"
entry="$install/app/bin/agent-autoloop.ts"
[[ -x "$node" && -r "$entry" ]] || fail "找不到已安装的 Autoloop CLI 引擎"

url="http://127.0.0.1:$port"
chmod 700 "$runtime"
if ! $resume; then mkdir "$runtime"/{hub,home,tmp,cwd,worker,workbench}; fi

cli_env=(env -i "HOME=$HOME" "TMPDIR=$runtime/tmp" "PATH=$PATH" "SHELL=/bin/sh")
if [[ -n ${CODEX_HOME:-} ]]; then cli_env+=("CODEX_HOME=$CODEX_HOME"); fi
cli() {
  local instance=$1
  shift
  "${cli_env[@]}" "AGENT_AUTOLOOP_HOME=$runtime/$instance" \
    "$node" --disable-warning=ExperimentalWarning "$entry" "$@"
}

if ! $resume; then
"$python" -B - "$runtime" "$worker_script" "$python" "$capacity" "$team" <<'PYRULES'
import json
import sys
from pathlib import Path

runtime, worker_script, python, capacity, team = sys.argv[1:]
capacity = int(capacity)
root = Path(runtime)
routes = [("odyssey-goal", "assignment", "Odyssey Goal")]
if team == "true":
    routes.extend([("odyssey-goal-implement", "implement", "Odyssey Goal Implement"),
                   ("odyssey-goal-analyze", "analyze", "Odyssey Goal Analyze")])
command = [python, "-B", worker_script, "worker",
           "--runtime", str(root / "workbench"), "--hub", str(root / "hub"), "--verify", "csv-v1"]
rules = {"sources": {"local": {"name": "Local"}}, "channels": {}, "agents": {}, "rules": []}
for agent, event, label in routes:
    rules["agents"][agent] = {"name": label, "worker": "local", "use": "command",
                              "timeout": 6, "command": command}
    rule = {
        "name": agent,
        "when": {"source": "local", "types": [f"workbench.goal.{event}"]},
        "to": agent,
        "cwd": str(root / "cwd"),
        "prompt": "Process the Workbench goal assignment.",
        "parallel": capacity,
        "tell": "never",
    }
    if team == "true":
        rule["group"] = "odyssey-goal"
    rules["rules"].append(rule)
(root / "hub" / "rules.json").write_text(json.dumps(rules) + "\n")
PYRULES

cli hub check >&2
owner_key=$(cli hub key new owner owner)
source_key=$(cli hub key new source local)
worker_key=$(cli hub key new worker local)
printf '{"url":"%s","hub":{"listen":"127.0.0.1:%s"},"owner":"%s","source":{"name":"local","key":"%s"}}\n' \
  "$url" "$port" "$owner_key" "$source_key" > "$runtime/hub/config.json"
printf '{"url":"%s","worker":{"name":"local","key":"%s","capacity":%s}}\n' \
  "$url" "$worker_key" "$capacity" > "$runtime/worker/config.json"
# 初始化成功后即可复用原数据库和密钥，包括首次监听失败的情况。
printf '%s\n' "$port" > "$runtime/.goal-pilot"
fi

lock="$runtime/.goal-launch"
mkdir "$lock" 2>/dev/null || fail "已有启动器持有 $lock"
pids=()
command_pid=
cleanup() {
  local pid
  trap '' INT TERM
  if [[ -n "$command_pid" ]]; then kill -TERM "$command_pid" 2>/dev/null || :; fi
  for pid in ${pids[@]+"${pids[@]}"}; do kill -TERM "$pid" 2>/dev/null || :; done
  for pid in ${pids[@]+"${pids[@]}"}; do wait "$pid" 2>/dev/null || :; done
  if [[ -n "$command_pid" ]]; then wait "$command_pid" 2>/dev/null || :; fi
  rmdir "$lock"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

start() {
  local instance=$1
  "${cli_env[@]}" "AGENT_AUTOLOOP_HOME=$runtime/$instance" \
    "$node" --disable-warning=ExperimentalWarning "$entry" up \
    > "$runtime/$instance/up.log" 2>&1 &
  pids+=("$!")
}

check_children() {
  local pid
  for pid in "${pids[@]}"; do
    kill -0 "$pid" 2>/dev/null || fail "Autoloop 进程 $pid 已退出，详见 $runtime/*/up.log"
  done
}

start hub
deadline=$((SECONDS + 15))
until /usr/bin/grep -Fq "[hub] listening on 127.0.0.1:$port" "$runtime/hub/up.log" &&
      /usr/bin/curl --noproxy '*' -fsS --max-time 1 "$url/healthz" >/dev/null 2>&1; do
  check_children
  (( SECONDS < deadline )) || fail "Hub 就绪超时，详见 $runtime/hub/up.log"
  sleep 0.2
done
start worker

deadline=$((SECONDS + 15))
until cli hub status --json | "$python" -B -c '
import json
import sys
try:
    online = {a["worker"] for a in json.load(sys.stdin)["agents"] if a["online"]}
except (ValueError, KeyError):
    sys.exit(1)
sys.exit(0 if set(sys.argv[1:]) <= online else 1)
' local; do
  check_children
  (( SECONDS < deadline )) || fail "Worker 就绪超时，详见 $runtime/*/up.log"
  sleep 0.25
done

check_children
printf 'READY\n'

if (( $# > 0 )); then
  AUTOLOOP_RUNTIME="$runtime/workbench" AUTOLOOP_HUB="$runtime/hub" "$@" &
  command_pid=$!
  while kill -0 "$command_pid" 2>/dev/null; do
    check_children
    sleep 0.25
  done
  status=0
  wait "$command_pid" || status=$?
  command_pid=
  exit "$status"
fi

while true; do
  check_children
  sleep 0.5
done
