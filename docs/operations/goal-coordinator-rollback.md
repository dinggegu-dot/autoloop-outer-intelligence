# 外层目标协调器回退手册

适用于通过正式 8765 工作台界面副本进入的外层目标协调器试点。试点在其他回环端口运行，使用各自的工作台、Hub、Worker 和工作目录。正式 `127.0.0.1:8765` 继续由现役看板提供服务。候选源代码以 V2 提交 `30e2987ad5c45d12ee929ec334f4e97f2d7571cc` 为回退基线，现役 8765 的程序与启动参数以其原安装为准。

## 暂停接单、排空并停机

1. 停止向试点提交、批准和续办工单，停用本轮专用提交脚本或试点桥入口。保留试点看板、目标调度进程、Hub 和 Worker，用当前工单卡暂停需要保留的执行。已完成工单可直接归档。
2. 刷新每张工单，核对外层工单、目标执行、派工事件和 Hub Run。外层工单显示「已暂停」后，继续等待目标执行从「正在停止」变为「已暂停」。核对原 Run 已到终态、该轮 Worker 派工子进程及 Codex 进程组已退出、活动派工为空。在途的接单研判、验收设计和目标协调调用也应结束。出现 `unknown`、Run 未终止或进程仍存活时，保留实例、日志和原工单，继续对账。草稿暂停时可保留持久队列，记录其状态供后续续办。
3. 使用原启动终端或本次试点的进程管理方式，先停试点看板，再停独立 `goal_runtime.py serve`，最后停试点 Worker 和 Hub。用包装启动器运行时，使用该启动器结束所属实例。复核试点回环监听和进程已退出，包括子进程。正式 8765 可用 `lsof -nP -iTCP:8765 -sTCP:LISTEN` 核对现役监听。
4. 停机后制作私有整树备份，记录候选版本、原目录、端口和启动参数。保留工作台 `tasks.sqlite3` 及伴随文件、工单工作区、`goal-attempts` 归档、Hub、Worker、启动标记和诊断日志。SQLite 单库备份可做在线取证。Hub、工作台和产物的一致恢复点以已排空且停机的整树为准。

## 核对路径和恢复点

- 看板的 `--runtime` 决定 `tasks.sqlite3` 与工单产物位置。试点启动器使用 `$pilot/workbench`。看板的 `--hub-home`、独立调度进程的 `--hub` 和 Hub 启动进程的 `AGENT_AUTOLOOP_HOME` 必须指向同一个 `$pilot/hub`。Worker 自身的 `AGENT_AUTOLOOP_HOME` 指向 `$pilot/worker`。查看本次启动参数、标记和日志来确认绝对路径。
- 安装的 Autoloop CLI 默认以 `~/.agent-autoloop` 为数据根，那里也放有 `app` 与 `runtime`。试点复用安装程序，使用显式的数据根。备份试点时从实际 Hub 根取 `hub.db`、数据库伴随文件、`master.key`、`config.json`、`rules.json`，以及存在时的 `secrets.env`。Worker 配置和私有桥数据位于各自目录，若本轮启用也应留存。飞书、其他本机服务及正式实例沿用其原目录。
- 保留停机整树作为恢复点。Hub CLI 的 `backup` 用 SQLite 备份 API 读取 WAL 中已提交的页，并复制同一实例的密钥与配置。单独复制 `hub.db` 或 `tasks.sqlite3` 会漏掉仍在 WAL 的提交。密钥、配置、工单、归档和日志均留在本机私有目录。

以下命令用于已停机的**合成试点**，将占位路径替换为实际目录。在同一个终端执行。恢复目标由命令新建，留作离线核验。

```bash
set -eu
umask 077
pilot=/private/tmp/替换成已停机的试点目录
test -f "$pilot/.goal-pilot"
test -f "$pilot/workbench/tasks.sqlite3"
test -f "$pilot/hub/hub.db"
backup=$(mktemp -d /private/tmp/autoloop-goal-backup.XXXXXX)
mkdir "$backup/snapshot" "$backup/rehearsal" "$backup/hub-export" "$backup/hub-restored"
cp -pR "$pilot/." "$backup/snapshot/"
cp -pR "$backup/snapshot/." "$backup/rehearsal/"

node="$HOME/.agent-autoloop/runtime/node-v24.22.2/bin/node"
entry="$HOME/.agent-autoloop/app/bin/agent-autoloop.ts"
AGENT_AUTOLOOP_HOME="$backup/rehearsal/hub" \
  "$node" --disable-warning=ExperimentalWarning "$entry" backup "$backup/hub-export"
AGENT_AUTOLOOP_HOME="$backup/hub-restored" \
  "$node" --disable-warning=ExperimentalWarning "$entry" restore "$backup/hub-export" --yes
cmp -s "$backup/rehearsal/hub/master.key" "$backup/hub-restored/master.key"

sqlite3 "$backup/hub-restored/hub.db" <<'SQL'
PRAGMA query_only=ON;
PRAGMA quick_check;
SELECT state, count(*) FROM runs WHERE rule = 'odyssey-goal' GROUP BY state;
SQL

sqlite3 "$backup/rehearsal/workbench/tasks.sqlite3" <<'SQL'
PRAGMA query_only=ON;
PRAGMA integrity_check;
SELECT json_extract(body,'$.status'), count(*) FROM outer_tickets GROUP BY 1;
SELECT json_extract(body,'$.status'), count(*) FROM tasks GROUP BY 1;
SQL
printf '%s\n' "$backup"
```

两项数据库检查均应返回 `ok`。逐项对照停机前记录的工单状态、Run 与保留产物摘要。Hub 有依赖运行时 `card_hash` 函数的索引，离线 SQLite 使用 `quick_check`。两项 SQLite 查询均在演练副本上执行，允许 SQLite 在各自的临时目录准备 WAL 伴随文件，连接内设为只读查询。CLI 的恢复会保存所替换的文件到目标目录下的 `restore-before-*`。

## 回到上一版与续办

- 撤掉候选试点的接单入口后，用户回到正式 8765。若需验证 V2 基线，在独立目录使用已取得的 `30e2987ad5c45d12ee929ec334f4e97f2d7571cc`，用基线自己的启动参数和单独数据目录做验证。候选数据库、工单契约、授权与产物继续留在候选恢复点中。
- 需要恢复候选试点时，在原绝对路径和原端口用候选版本重启。启动标记、Hub 路由和批准绑定含原路径。重定位备份适合离线读验。运行恢复先对账同一工单、派工、Run、预算和检查点，用户再从暂停的当前版本续办。下载归档后核对摘要，并核对原授权和派工记录各只出现一次。
- 若原试点目录已被后续运行写入，先将当前现场另存为一个独立恢复点，核对两边的新增工单、Run、配置与产物，再由恢复负责人选择恢复到哪个时点。正式实例的停机、覆盖和实际部署需要单独授权。

恢复冷备份时，先确认同一试点的看板、调度器、Hub 和 Worker 已停止，两个回环端口没有监听。将停机现场移到同一父目录下的独立位置，保留未改写的备份，将备份复制回原路径。检查工作台 `integrity_check`、Hub `quick_check` 以及授权、配置和已验产物的字节摘要。用隔离启动器的 `--resume` 在原 Hub 端口启动 Hub 和 Worker，再按原参数启动调度器和看板。回读原单，确认暂停状态和历史派工保持一致后，从原单选择「继续此单」。当前版本的续办将使用原授权核验暂停时的检查点。

## 已完成工单的离线恢复

2026-11-23，以正式 8765 当前已安装的源码构建隔离副本，通过普通 `POST /api/tasks` 入口完成合成文件名索引工单。停机后对隔离目录整树备份，在私有临时副本重跑本页命令。Hub CLI 备份和恢复成功，主密钥字节一致。恢复 Hub 的 `quick_check` 与工作台的 `integrity_check` 均返回 `ok`。

- 原工单 `wb-3c554511ccc9409496710310426d890b` 回读为 `AUTHORIZED`，授权版次为 1，目标执行状态为 `completed`。恢复 Hub 中 5 条 `odyssey-goal` Run 均为 `done`，Run ID 逐一对应工作台的 5 条派工。
- 用恢复副本中的 `search.py` 和 `update.py` 字节重算文件摘要及归档清单 SHA-256，得到 `d8f50db40aad5613ef615e8f9e10cf20dc77211e35575d457857272ec0138f61`，与原归档、工单交付记录和隔离验收记录一致。
- 正式 `127.0.0.1:8765` 在演练前后均由同一进程监听。

早期演练以工作区版本构建隔离副本。已交付工单 `wb-e4133c074a164b1e81e390ec0167fbf7` 读回 5 条已完成 Run，归档清单 SHA-256 为 `018fa52a23aab5b7678207537bf788dcca85ff9ab751e7cd4cab713a27dcb94f`。另一草稿演练保留了原 `VERIFICATION_GAP` 工单及其零目标执行、零 Run 状态。

## 活动工单的冷备恢复与续办

2026-11-23，使用 V2 候选 `a4245cff4d2c2cc97a928ab9284ea43fced77d09` 和正式 8765 的已安装源码，在隔离副本通过普通新单接口提交合成文件名索引工单 `wb-3922d504142547929d25b3e5fbc6f008`，只批准一次。

1. Run 3 独立验收并归档 `search.py`。在第二阶段的真实 Codex 正执行 Run 4 时，从原工单暂停。Run 4 由 `claimed` 转为 `cancelled`，原进程组退出，活动派工和外层在途调用归零，目标执行到达 `paused`。
2. 停止看板、调度器、Worker 和 Hub，确认两个隔离监听均关闭。保留停机原目录和独立冷备，将冷备复制回原路径。两库以新文件恢复，工作台 `integrity_check` 与 Hub `quick_check` 均为 `ok`。在原端口重启，续办前回读的工单、授权、预算、原 Run、检查点和第一阶段交付均与停机前一致。
3. 原单续办后，Run 5 先核验恢复的检查点，确认搜索条件仍通过、`update.py` 尚缺。Run 6 的目标协调调用四项只读 MCP 工具，要求补齐增量更新并保留已验的搜索文件。Run 7 修复，Run 8 独立终验 4/4 通过。两项未见合成输入也在独立进程验证通过。
4. 通过原单下载两份脚本，字节摘要与终验归档一致，`search.py` 与第一阶段的已验版本相同。批准账本只有一次，重放 Run 8 原始终验回执未产生新派工。正式 8765 在隔离试点停机前、停机中和验收后均由同一进程监听。

首版取证脚本误读了终验等级字段。随后的取证读取同一张已完成工单和原始回执，并补做 HTTP 下载与回执重放校验。备份和运行日志留在本机。[脱敏验收记录](../../evidence/goal-coordinator/active-rollback.json)
