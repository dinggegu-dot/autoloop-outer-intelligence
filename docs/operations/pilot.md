# V2 隔离试点运行手册

## 启动

在 macOS 上从仓库根目录运行。准备 Python 3、Codex CLI，以及已安装的 Autoloop。启动器当前从 `~/.agent-autoloop/runtime/node-v24.22.2/bin/node` 和 `~/.agent-autoloop/app/bin/agent-autoloop.ts` 加载独立依赖。工作台的 Python CLI 黑盒检查还使用系统 Seatbelt。先运行 `python3 -B scripts/check.py`，记录候选提交和跑测结果。

选择两个空闲的回环端口。终端一创建空试点目录并启动 Hub、Worker。保留实际目录和端口用于后续命令。

```bash
pilot=$(mktemp -d /private/tmp/autoloop-v2-pilot.XXXXXX)
printf '%s\n' "$pilot"
bash 网页工作台/superagent/isolated_goal_hub.sh "$pilot" 43350
```

出现 `READY` 后启动独立协调器和看板。两个终端的 `pilot` 均须设为上一步生成的绝对路径。

```bash
# 终端二
python3 -B 网页工作台/superagent/goal_runtime.py serve \
  --runtime "$pilot/workbench" --hub "$pilot/hub"
```

```bash
# 终端三
python3 -B 网页工作台/server.py --port 43351 \
  --runtime "$pilot/workbench" --hub-home "$pilot/hub" \
  --goal-pilot --outer-intelligence
```

从王国地图进入「任务工作台」，或打开 [/workbench](http://127.0.0.1:43351/workbench)。如已有本机原版画册，可给看板命令追加 `--odyssey-assets "/绝对路径/assets/odyssey"`。

在浏览器打开 [本机试点看板](http://127.0.0.1:43351/)。看板处理 V2 接单与人工决定，协调器负责推进派工及对账。Hub 的 `workbench.goal.assignment` 路由指向本机 Worker。启动器命令含历史 `--verify csv-v1` 参数，V2 派工由内嵌批准绑定选择自己的验收契约。V2 使用每张工单的契约，启动参数中无需提供固定契约或启用 V1 团队试点。

查看页面和空工单状态只涉及本机。提交需求后，看板可启动真实 Codex 接单判断。待批准卡展示目标、限制、逐项验收与预算。批准后通过同一工单查看记录，并核对实际归档的文件和逐项证据。若需要独立飞书试点，为上述看板进程附加 `--feishu-socket` 和 `--feishu-decision-key`，使用本人私有目录与独立桥配置，密钥要求为本人拥有且无组或其他用户权限的 32 字节文件。现役飞书桥和现役看板使用各自的目录与配置。

## 停机和回到上一版

1. 停止提交新工单，从当前工单卡请求暂停需要保留的活动任务。让协调器、Hub 和 Worker 继续运行，核对关联 Run 的终态、原进程退出及工单状态。中台状态未知时保留原现场供对账。
2. 依次停止看板、协调器和隔离启动器。使用前台进程的正常终止途径，保留整个试点目录和候选源代码版本。快照包含 `workbench`、`hub`、`worker` 及启动标记，并应存放在私有目录。里面包含可能敏感的工单、资料、回执、日志和实例密钥。
3. 在单独目录从备份副本检查 SQLite 和文件清单。停机后可以备份整树。做在线 SQLite 副本时使用备份 API，跨 Hub 和工作台的一致恢复点仍以已排空并停机的整树为准。
4. 回到旧版时按旧版自己的程序、目录、端口和桥配置启动。V2 工单和新授权仍保留在 V2 的快照。试点与旧版实例各自保存操作记录。

## 原现场恢复和副本演练

协调器单独退出时，保持原 Hub、Worker、看板和工作目录，用同一条 `goal_runtime.py serve` 命令重启协调器。核对同一工单、原事件、Run、派工和现有检查点。原轮次仍在运行时等待其状态更新，避免再次发送同一业务操作。已确认停止的工单由用户从当前版本续办。

整套试点正常停机后，可用原路径与原端口重启 Hub、Worker，然后重启协调器和看板。

```bash
bash 网页工作台/superagent/isolated_goal_hub.sh --resume "$pilot" 43350
```

启动器的 `--resume` 校验原端口和 `.goal-pilot` 标记。配置保存绝对路径，Mandate 也绑定工作区绝对路径。将备份复制到新目录时，可先检查数据可读性。直接启动重定位副本会让路由指向旧目录或让授权校验拒绝，完整恢复演练使用事先规划的独立实例和其原路径。若强杀启动器遗留 `.goal-launch`，在核实原实例进程均已退出、监听已关闭后可移除该试点目录下的空锁目录，再用原路径恢复。

隔离读验包括在副本执行 SQLite `PRAGMA integrity_check` 或 `quick_check`，并对照检查点清单核验文件摘要。运行恢复的验收点包括读取原工单、识别原 Run、避免重复授权与派工，以及下载字节保持已验收摘要。旧版真实恢复证据和本次 V2 测试结论分别以[进度记录](../progress/v2-status.md)和[验收矩阵](../testing/acceptance-matrix.md)为准。

## 私有仓库 CI 与费用

本机完整检查使用 `GOAL_CHECKS_REAL_SEATBELT=1 python3 -B scripts/check.py`。确认 GitHub Actions 当前包含的额度足够后，将待测 PR 的同一提交推到专用 `ci/included/` 分支，GitHub 即为该 SHA 运行一次 macOS 检查，单次作业最多五分钟。普通功能分支推送不会触发托管作业。专用分支和 Actions 记录保留可回查的证据。若账户额度不可核实，继续使用本机入口。

```bash
commit=$(git rev-parse HEAD)
git push origin "HEAD:refs/heads/ci/included/$commit"
```

## 借用已建飞书私聊做真机验收

已有本人私聊桥需使用仓库所示的消息持久化与共享点击结构。现役监听继续接收事件。试验进程只取当前本人、时间窗与随机前缀匹配的消息，以及回复本次已投递卡片的文字或按钮。候选桥通过同一个机器人给本人回执，试验状态保存在单独目录。旧桥对首条消息也可能发来归属提示，后续认准含 V2 工单号和版本的卡片。

建立试验目录并生成试验专用的决定密钥、开始时间和筛选前缀。下面三项用独占创建写入新目录。

```bash
stage=$(mktemp -d /private/tmp/autoloop-feishu-test-XXXXXXXX)
python3 - "$stage" <<'PY'
from datetime import datetime, timezone
import os, secrets, sys
from pathlib import Path
os.umask(0o077)
root=Path(sys.argv[1])
for name,content in [('decision',secrets.token_bytes(32)),
                     ('started',datetime.now(timezone.utc).isoformat().encode()),
                     ('prefix',('V2验收-'+secrets.token_hex(6).upper()).encode())]:
    with (root/name).open('xb') as f:
        f.write(content)
print((root/'prefix').read_text())
PY
```

按启动章节建立独立 Hub 和协调器，启动候选看板时另外传入 `--feishu-socket "$stage/board.sock" --feishu-decision-key "$stage/decision"`。在第四个终端指定同一试验目录及现役设置文件的绝对路径。

```bash
python3 -B scripts/live_feishu.py "$stage" "$bridge_settings"
```

在现有本人机器人私聊发“随机前缀 + 空格 + 合成需求”。测试目录时发“随机前缀 + 空格 + /tasks”。试点投递按本次前缀独立去重。查看目标、验收、预算，核对后点候选卡批准。也可在同一工单的本机看板批准。结束后停掉本次前台进程并保留验收副本。试验收件窗口最长一小时。飞书 App 配置、用户凭据、现役数据库均留在本机。
