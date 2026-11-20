#!/bin/bash
set -euo pipefail
umask 077
# 用独立运行目录和合成材料验证原生 Hub / Worker。只授权仓库随附的小样。
[[ $# == 4 ]] || { echo 'usage: bash pilot.sh EMPTY_PRIVATE_TMP AFR_CHECKOUT AFR_PYTHON PORT' >&2; exit 2; }
root=$(cd -- "$1" && pwd -P)
[[ "$root" == /private/tmp/* && -O "$root" && -z $(ls -A "$root") && "$4" =~ ^[0-9]+$ ]] || exit 2
integration=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
afr_checkout=$2
afr_py=$3
port=$4
install="$HOME/.agent-autoloop"
node="${AFR_PILOT_NODE:-$install/runtime/node-v24.22.2/bin/node}"
entry="${AFR_PILOT_ENTRY:-$install/app/bin/agent-autoloop.ts}"
url="http://127.0.0.1:$port"
cli() {
    local instance=$1
    shift
    AGENT_AUTOLOOP_HOME="$root/$instance" "$node" "$entry" "$@"
}
start() {
    local instance=$1
    export AGENT_AUTOLOOP_HOME="$root/$instance"
    exec "$node" "$entry" up
}
chmod 700 "$root"
mkdir "$root"/{hub,worker,rag,inbox,vault,state,cwd}
cp -R "$afr_checkout/afr_agent" "$root/rag/"
mkdir "$root/hub/plugins"
cp -R "$integration/plugins/afr-drop" "$root/hub/plugins/"
"$afr_py" -B - "$root" "$integration" "$afr_py" "$url" <<'PY'
import hashlib, json, secrets, sys
from pathlib import Path
root, integration, python, url = sys.argv[1:]
root, integration = Path(root), Path(integration)
rules = json.loads((integration/'rules.fragment.json').read_text())
config = root/'local.json'
rules['agents']['afr-process']['command'] = [python, '-B', str(integration/'route.py'), 'process', '--config', str(config)]
rules['rules'][0]['cwd'] = str(root/'cwd')
(root/'hub'/'rules.json').write_text(json.dumps(rules))
secret = secrets.token_hex(32)
(root/'state'/'source.secret').write_text(secret)
(root/'hub'/'secrets.env').write_text('AGENT_AUTOLOOP_SECRET_AFR_DROP='+secret+'\n')
approved = hashlib.sha256((integration/'synthetic.md').read_bytes()).hexdigest()
(root/'state'/'approvals.json').write_text(json.dumps({'destination':'OpenAI Codex','sha256':[approved]}))
config.write_text(json.dumps(dict(state=str(root/'state'),inbox=str(root/'inbox'),vault=str(root/'vault'),
                                 cwd=str(root/'rag'),python=python,approvals=str(root/'state'/'approvals.json'),
                                 source_url=url+'/v1/in/afr-drop',source_secret=str(root/'state'/'source.secret'))))
PY
cli hub check
owner=$(cli hub key new owner owner)
worker=$(cli hub key new worker local)
printf '{"url":"%s","hub":{"listen":"127.0.0.1:%s"},"owner":"%s"}\n' "$url" "$port" "$owner" > "$root/hub/config.json"
printf '{"url":"%s","worker":{"name":"local","key":"%s","capacity":1}}\n' "$url" "$worker" > "$root/worker/config.json"
(start hub) > "$root/hub/up.log" 2>&1 &
hub_pid=$!
worker_pid=
watch_pid=
clean() {
    for pid in "$watch_pid" "$worker_pid" "$hub_pid"; do
        [[ -z "$pid" ]] || kill "$pid" 2>/dev/null || true
    done
    wait || true
}
trap clean EXIT
ready=false
for _ in {1..80}; do
    if cli hub status --json > "$root/state/hub-status.json" 2>/dev/null; then
        ready=true; break
    fi
    sleep 0.3
done
"$ready" || exit 1
(start worker) > "$root/worker/up.log" 2>&1 &
worker_pid=$!
run_route() { "$afr_py" -B "$integration/route.py" "$1" --config "$root/local.json"; }
"$afr_py" -B "$integration/route.py" watch --config "$root/local.json" > "$root/state/observer.log" &
watch_pid=$!
external="${AFR_PILOT_EXTERNAL_DROP:-0}"
echo "READY inbox=$root/inbox vault=$root/vault"
if [[ "$external" != 1 ]]; then
"$afr_py" -B - "$root" "$integration" <<'PY'
import json, subprocess, sys, time
from pathlib import Path
root, integration = map(Path,sys.argv[1:])
blob = (integration/'synthetic.md').read_bytes()
inbox = root/'inbox'
with (inbox/'Synthetic-42.md').open('wb') as out:
    cut = blob.index(b'\n')+1
    out.write(blob[:cut])
    out.flush()
    time.sleep(1)
    run = subprocess.check_output([sys.executable,'-B',str(integration/'route.py'),'scan','--config',str(root/'local.json')])
    unsettled = json.loads(run)
    assert unsettled['settling_count'] == 1 and unsettled['submitted_count'] == 0, unsettled
    print('unsettled',unsettled)
    out.write(blob[cut:])
(inbox/'Synthetic-local-only.md').write_text('# 独立本地合成小样\n\n这个版本没有获得出站许可。\n')
PY
fi
published=false
for ((check=0; check<${AFR_PILOT_WAIT_CYCLES:-150}; check++)); do
    if "$afr_py" -B - "$root" <<'PY'
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
sha = json.loads((root/'state'/'approvals.json').read_text())['sha256'][0]
path = root/'state'/(sha+'.json')
if not path.exists():
    sys.exit(1)
data = json.loads(path.read_text())
if data['state'] != 'published':
    sys.exit(1)
print(json.dumps({'state':data['state'],'attempt':data['attempt'],'sha256':sha}))
PY
    then published=true; break; fi
    sleep 2
done
"$published" || exit 1
run_route scan
if [[ "$external" != 1 ]]; then
"$afr_py" -B - "$root" <<'PY'
import hashlib, json, subprocess, sys
from pathlib import Path
root = Path(sys.argv[1])
vault = root/'vault'
denied = hashlib.sha256((root/'inbox'/'Synthetic-local-only.md').read_bytes()).hexdigest()
approved = json.loads((root/'state'/'approvals.json').read_text())['sha256'][0]
read = subprocess.run([sys.executable, '-B','-m','afr_agent','process','--vault',str(vault),'--sha256',denied],
                      cwd=root/'rag',capture_output=True,text=True)
assert read.returncode == 2 and json.loads(read.stdout)['error'] == 'local_only'
boards = list(vault.rglob('文章白板.canvas'))
originals = list(vault.rglob('原文结构.canvas'))
assert len(boards)==1 and len(originals)==2
assert len(list(vault.rglob('完整阅读.md')))==1
assert approved[:16] in boards[0].parent.name
print('authorization', 'local_only', 'initial_boards',len(originals),'published_boards',len(boards))
PY
fi
hub_done=false
for _ in {1..30}; do
    cli hub status --json > "$root/state/hub-status.json"
    if "$afr_py" -B - "$root" <<'PYINNER'
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
runs = json.loads((root/'state'/'hub-status.json').read_text())['runs']
if len(runs) != 1 or runs[0]['state'] != 'done':
    sys.exit(1)
assert '"state": "published"' in runs[0]['summary']
assert all(value not in json.dumps(runs,ensure_ascii=False) for value in ['Synthetic','机柜样本','白板.canvas'])
print('hub_runs',len(runs),'state',runs[0]['state'],'privacy', 'digests_and_status_only')
PYINNER
    then hub_done=true; break; fi
    sleep 1
done
"$hub_done"
