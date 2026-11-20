# AFR 本机投放接线

观察器启动即补扫，每秒观察固定 inbox 的路径、文件大小、mtime 和 ctime。合并短时变动，等待稳定 4 秒后调用 AFR scan --settle-seconds 4。扫描期间目录变化会取消本次派工，下一次重新核对。60 秒补扫用于恢复暂停、断线或错过的变更。初读由 AFR 本机生成。

固定配置记录 Python、AFR 源码目录、互不包含的 inbox 与 vault、私有状态目录、许可清单与本机签名入口。部署时把 plugins/afr-drop 放到目标 Hub 插件目录，合并 rules.fragment.json 并填写绝对路径。Hub 使用 AGENT_AUTOLOOP_SECRET_AFR_DROP，观察器从本地文件读取同值。试点启动器只写显式指定的空 /private/tmp 实例，无后台安装。

只有 AFR pending SHA 与精确许可的交集可派工。许可文件归当前用户所有且仅本人可访问，格式为 {"destination":"OpenAI Codex","sha256":["…"]}。生产默认空列表。Worker 每次调用前重复核对，并将许可传给 AFR。传给 Hub 的事件载荷仅有 SHA、尝试次数和重启代号。正式执行回执只含状态、SHA 和有限错误码。本机原件路径、正文和白板路径留在 AFR。

Hub 使用稳定事件 ID 去重，并按 SHA 各自排队，进程组超时由原生 Worker 处理。Worker 在固定版本锁下保留本地处理记录。常见暂态失败在 60 秒、300 秒退避后重试，最多三次。凭证问题、模型不可用、OCR、用户编辑冲突和其他需人工核对的问题会保留待办状态。先排障，再以 route.py rearm --config CONFIG --sha256 EXACT_SHA 对仍获授权的单个版本重新启用。撤销许可后，已排队任务也会被拦下。完成的版本由 AFR 和 Worker 层共同跳过。

## 复跑

需要当前 AFR Python 环境、支持 --settle-seconds 与 --approvals 的 AFR v1 源码、已安装的 Autoloop 和可用的 Codex 登录。AFR_PILOT_NODE 与 AFR_PILOT_ENTRY 可替换本机 Autoloop 路径。

    trial=$(mktemp -d /private/tmp/afr-drop-trial.XXXXXX)
    bash pilot.sh "$trial" /absolute/afr-checkout /absolute/afr-python 43994
    python3 -B test_route.py

自动试点分段写入随附合成文档，另投放一份无许可合成文档，检查分段写入延后、两份本机初读、唯一获许可版本完成白板、无许可版本返回 local_only、补扫无重复派工以及 Hub 无材料载荷。

设 AFR_PILOT_EXTERNAL_DROP=1 可先启动空 vault，等 READY 后将 synthetic.md 原字节复制到 inbox，再测量 Obsidian 实际打开与点击。磁盘落盘时间单独记录。

## 实运行证据

- 2026-11-20 的分段写入试点在写入未结束时返回 settling_count=1、pending_count=0、submitted_count=0。稳定后原生 Hub 收到一条事件，Worker 使用现有 Codex 登录并完成 run 1。两份合成材料生成初读，仅获许可版本生成文章白板与完整阅读。未获许可版本的 AFR CLI 返回 exit 2、local_only。补扫仅剩未授权 pending，派工为零。
- 同日联合外投于 13:14:08.601494 落盘，超过 4.543 秒出现原文结构板，15.863 秒出现完整文章板。以上为磁盘观测。测试稿启动器在 Worker 保存原回执后、Hub 接收前提前关闭，保留原试点目录重启后，原 Hub run 1 在 13:15:54.058 持久化为 done，自动重派次数 0，还是原 published 回执。启动器随后改成等待 Hub done，启动 Worker 前先等 Hub 可用。
- 修复后的启动器在新隔离实例完整复跑，退出码为 0，Hub 恰好一轮且持久化为 done。四项本机回归通过，覆盖变动中的抑制、退避、撤销后重获精确许可及重新启用时拒绝已排队的旧事件。

## 仅本地初读

正式资料可使用单独的本地配置。state 为当前用户独占的 0700 目录，inbox 和 vault 是已经存在且互不包含的绝对路径。cwd 是含 afr_agent 的可信 AFR 仓库，python 保留项目虚拟环境的解释器路径。已存在旧 AFR 数据库时可指定 legacy_db，AFR 以只读方式打开。示例路径请替换为实际本机路径。

```json
{
  "local_only": true,
  "inbox": "/absolute/inbox",
  "vault": "/absolute/vault",
  "state": "/absolute/private-state",
  "cwd": "/absolute/afr-checkout",
  "python": "/absolute/afr-checkout/backend/.venv/bin/python",
  "approvals": "/absolute/private-state/approvals.json"
}
```

首次可用 `python3 -B route.py scan --config /absolute/private-state/local.json` 核对新增、等待写稳、不支持格式及错误类别的数量。持续运行用 `python3 -B route.py watch --config /absolute/private-state/local.json`。停止这个观察器即可撤回本地自动扫描，已发表的本地文件由本人保留。local_only 为 true 时，即使许可清单已有某个 SHA，也不会向 Hub 提交模型派工。后续授权由资料所有人针对实际版本单独决定。

本机看板添加 `--afr-root /absolute/afr-checkout --afr-python /absolute/afr-checkout/backend/.venv/bin/python --afr-vault /absolute/vault`，工作台导航会显示「AFR 本机查询」。停用时从该实例的启动参数移除这三项，再按原配置启动。正式 V2 仍可沿用它自己的运行目录、Hub 与飞书配置。[查询与权限](../../scripts/AFR-PANEL.md)
