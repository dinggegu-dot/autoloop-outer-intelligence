#!/bin/bash
set -euo pipefail
umask 077

fail() {
  printf '%s\n' "$*" >&2
  exit 1
}

resume=false
if [[ ${1:-} == --resume ]]; then resume=true; shift; fi
if [[ $# -lt 1 || $1 != /* || ! -d $1 || -L $1 ]]; then
  fail "用法：$0 [--resume] /绝对/试点目录 [端口 [-- 命令 参数...]]"
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
[[ -O "$runtime" ]] || fail "runtime 必须由当前用户拥有"
if $resume; then
  [[ "$runtime" == /private/tmp/* && -f "$runtime/.team-pilot" && ! -L "$runtime/.team-pilot" &&
     $(<"$runtime/.team-pilot") == "$port" && -f "$runtime/hub.json" ]] || fail "恢复仅允许原端口对应的临时试点目录"
else
  [[ -z $(/bin/ls -A "$runtime") ]] || fail "新运行的 runtime 必须为空"
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
router="$script_dir/router.py"
[[ -f "$router" && -r "$router" ]] || fail "找不到可读的 $router"
python=$(command -v python3) || fail "找不到 python3"
[[ "$python" == /* && -x "$python" ]] || fail "python3 必须是 PATH 中的可执行文件"
install="$HOME/.agent-autoloop"
node="$install/runtime/node-v24.22.2/bin/node"
entry="$install/app/bin/agent-autoloop.ts"
[[ -x "$node" && -r "$entry" ]] || fail "找不到已安装的 Autoloop CLI 引擎"

slots=(aggregate reconcile review)
url="http://127.0.0.1:$port"
chmod 700 "$runtime"
if ! $resume; then mkdir "$runtime"/{hub,home,tmp,cwd,aggregate,reconcile,review}; fi

cli_env=(env -i "HOME=$runtime/home" "TMPDIR=$runtime/tmp" "PATH=$PATH" "SHELL=/bin/sh")
if [[ -n ${CODEX_HOME:-} ]]; then cli_env+=("CODEX_HOME=$CODEX_HOME"); fi
cli() {
  local instance=$1
  shift
  "${cli_env[@]}" "AGENT_AUTOLOOP_HOME=$runtime/$instance" \
    "$node" --disable-warning=ExperimentalWarning "$entry" "$@"
}

if ! $resume; then
"$python" -B - "$runtime" "$router" "$python" "${slots[@]}" <<'PY'
import json
import sys
from pathlib import Path

runtime, router, python, *slots = sys.argv[1:]
root = Path(runtime)
rules = {"sources": {"team": {"name": "Team"}}, "channels": {}, "agents": {}, "rules": []}
for slot in slots:
    name = f"team-{slot}"
    cwd = root / "cwd" / slot
    cwd.mkdir(mode=0o700)
    rules["agents"][slot] = {
        "name": name, "worker": slot, "use": "command", "timeout": 6,
        "command": [python, "-B", router, "worker", "--runtime", runtime, "--capability", slot],
    }
    rules["rules"].append({
        "name": name, "group": name, "when": {"source": "team", "types": [f"team.{slot}"]},
        "to": slot, "cwd": str(cwd), "prompt": f"Process the {slot} event.",
        "parallel": 1, "tell": "never",
    })
(root / "hub" / "rules.json").write_text(json.dumps(rules) + "\n")
PY

cli hub check >&2
owner_key=$(cli hub key new owner owner)
source_key=$(cli hub key new source team)
printf '{"url":"%s","hub":{"listen":"127.0.0.1:%s"},"owner":"%s"}\n' \
  "$url" "$port" "$owner_key" > "$runtime/hub/config.json"
for slot in "${slots[@]}"; do
  worker_key=$(cli hub key new worker "$slot")
  printf '{"url":"%s","worker":{"name":"%s","key":"%s","capacity":1}}\n' \
    "$url" "$slot" "$worker_key" > "$runtime/$slot/config.json"
done
fi

pids=()
command_pid=
cleanup() {
  local pid
  trap '' INT TERM
  if [[ -n "$command_pid" ]]; then kill -TERM "$command_pid" 2>/dev/null || :; fi
  for pid in ${pids[@]+"${pids[@]}"}; do kill -TERM "$pid" 2>/dev/null || :; done
  for pid in ${pids[@]+"${pids[@]}"}; do wait "$pid" 2>/dev/null || :; done
  if [[ -n "$command_pid" ]]; then wait "$command_pid" 2>/dev/null || :; fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

start() {
  local instance=$1
  local agent_home="$runtime/home"
  if [[ "$instance" != hub ]]; then agent_home="$HOME"; fi
  "${cli_env[@]}" "HOME=$agent_home" "AGENT_AUTOLOOP_HOME=$runtime/$instance" \
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
for slot in "${slots[@]}"; do start "$slot"; done

deadline=$((SECONDS + 15))
until cli hub status --json | "$python" -B -c '
import json
import sys
try:
    online = {a["worker"] for a in json.load(sys.stdin)["agents"] if a["online"]}
except (ValueError, KeyError):
    sys.exit(1)
sys.exit(0 if set(sys.argv[1:]) <= online else 1)
' "${slots[@]}"; do
  check_children
  (( SECONDS < deadline )) || fail "Worker 就绪超时，详见 $runtime/*/up.log"
  sleep 0.25
done

check_children
if ! $resume; then
printf '{"url":"%s","source_key":"%s","owner_key":"%s"}\n' "$url" "$source_key" "$owner_key" > "$runtime/hub.json.tmp"
mv "$runtime/hub.json.tmp" "$runtime/hub.json"
printf '%s\n' "$port" > "$runtime/.team-pilot"
fi
printf 'READY\n'

if (( $# > 0 )); then
  AUTOLOOP_RUNTIME="$runtime" AUTOLOOP_HUB_JSON="$runtime/hub.json" "$@" &
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
