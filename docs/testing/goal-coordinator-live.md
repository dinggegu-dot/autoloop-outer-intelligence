# 外层 Agent 首批隔离验收

2026-11-23。本轮在正式工作台 8765 当前运行的安装源码副本运行。副本保留正式首页和普通建单表单，新建时显式选择「使用智能工单试点」。Hub、Worker、工作台数据库及工作区使用新建的本机隔离目录和回环端口。实际执行从经会话鉴权的 `POST /api/tasks` 开始，通过原工单批准、读取调用和下载。验收脚本调用生产协调循环的单步入口来安排故障时序。Codex、Hub、Worker、工作台和独立 Seatbelt 验证器均实际运行。

## 开发基线

| 对象 | 核对结果 |
| --- | --- |
| 本地 V2 checkout | `867f138e0601ed9bb26d49268c4f33992f94c9b9`，保留原 checkout 的未提交文档 |
| GitHub V2 | `30e2987ad5c45d12ee929ec334f4e97f2d7571cc`，相对本地基线仅更新 README |
| GitHub main | `e14dd1c2d970deb44fe2f794901ee7f4c86d2744` |
| 在途项 | #3 连续执行验收、#7 首版交付总单仍开放。#4 为 V2 候选 PR，#8 和 #10 为并行在途工作。 |
| 本轮代码 | 本地 `codex/goal-coordinator-tools`，功能提交 `a5cc5a4`，安装源码隔离与验收策略提交 `c7d4956`、`98cdcc4` |

[工单 #3](https://github.com/dinggegu-dot/autoloop-outer-intelligence/issues/3) · [总单 #7](https://github.com/dinggegu-dot/autoloop-outer-intelligence/issues/7) · [V2 PR #4](https://github.com/dinggegu-dot/autoloop-outer-intelligence/pull/4)

## 正常交付

接单研判和验收设计均为真实 Codex。原工单只生效一次批准，旧版批准请求返回 HTTP 409。Hub 共记录五个执行 Run，其中三个调用真实 Codex，另两次是独立验证。第一阶段 4 项通过，最终 8 项通过。通过下载入口读回的两份文件与归档摘要相符。验收者另用两个新的合成输入检查文件名前缀与增量去重、删除，两个检验均通过。无协调调用。[对照证据](../../evidence/goal-coordinator/control.json)

## Worker 重启与跨阶段回归

| 验收步骤 | 从原工单核对的结果 |
| --- | --- |
| 实施中断 | 阶段二 Run 4 已被真实 Worker 领取，模型完成文件写入，业务回执尚未提交。测试夹具在隔离目录改坏名称查询，随后重启实际 Worker，并受控丢失该 Run 的业务回执。Hub 将原 Run 结算为 `nothing`。引擎从原 Run 恢复一次检查点，恢复摘要等于故障检查点。 |
| 拒收失败版本 | Run 5 检出前序查询 4 项失败，增量更新 4 项仍通过。查询当前交付及显式查询失败候选均返回 HTTP 404。 |
| 真实目标协调 | Run 6 的 gpt-6.2-sol 读取了失败证据、已验产物、计划版本和预算。四项原生 MCP 事件与服务端证据哈希逐项相符。提案要求修复查询、保留 `update.py` 的精确版本，并在阶段二按全契约复验。 |
| 沿用唯一授权 | 引擎在同一事务登记采用决定及待派工，后续实际运行 Run 7 修复和 Run 8 独立终验。接受的授权为 v1。重放原建单、原批准，以及已落盘的同一协调回执，原工单的派工清单保持八条。 |
| 复验与交付 | Run 8 的 8 项检查全部通过，归档下载字节与账本一致。保留的 `update.py` SHA-256 为 `3c2812259aa48a5a234a8092810580dd975d9a4c1f402b7496753434a2b1d139`。交付清单 SHA-256 为 `67793a79d52e1aa387cfda0ea9946accd86f92b61f5eff8a6a0a2313d4316b7a`。另两组未列入工单的合成输入通过。 |

原回归归档的完整清单含 `xcrun_db`，证据保留其文件名和摘要。仓库中的两份合成脚本另有独立清单摘要 `95970dc206c2d4cd3cace5b793cd62b39f134d49307648ec9bc4c516883ad3b3`。

[回归证据及原工单调用摘要](../../evidence/goal-coordinator/regression.json) · [已验查询源码](../../evidence/goal-coordinator/final-artifact/search.py) · [已验增量更新源码](../../evidence/goal-coordinator/final-artifact/update.py)

## 暂停与迟到提案

正式安装源码副本的暂停案例由真实 Codex Run 6 完成四项 MCP 读取。故障候选的四项名称查询验收失败，四项增量验收通过，故障版本下载返回 HTTP 404。Codex 形成提案后，测试 Worker 在提交业务回执前等待。原工单通过真实暂停端点进入暂停，随后放行原回执。调用详情记录提案已过期，后续派工数为零，前序已验版本仍可追溯。[暂停证据](../../evidence/goal-coordinator/pause-stale.json)

## 独立复核与发布状态

独立复核使用 GPT-6.2 Sol 高强度推理。复核者分别核对了三次正式安装源码副本的验收结果、工作台和 Hub 的持久账本，并按原工单的版本重新计算工具证据与归档摘要，用另外的合成输入运行交付文件。全量本机回归 305 项通过，含真实 Seatbelt、工作台、飞书接口及 JavaScript 检查。

正式 8765 的监听进程前后相同。候选以 GitHub V2 PR #4 的分支为目标，供审查后决定正式放行。

## 正式 Chrome 验收

2026-11-23，从正式安装源码构建新的隔离副本。在正式 Google Chrome 点击普通「新建工单」，勾选智能试点并提交一张合成文件名索引工单。原工单 `wb-2d1ebe0a5aa74a0a9e70baff2b9e6690` 展示目标 v1、验收 v1。只批准一次，服务账本记录一个批准快照和一个授权 v1。

- Run 3 的四项查询检查通过。隔离夹具在阶段二将查询代码改坏，Run 5 发现四项查询失败，四项增量更新仍通过。这张 Chrome 工单专门验证浏览器入口和原单追溯。
- 在 Chrome 展开原单中的 Goal Coordinator，看到 Run 6 的采纳理由、受影响步骤、保留文件版本和八项复验。四项真的 Codex MCP 读取摘要已与服务端逐项核对。Run 7 修复和 Run 8 独立终验归在原授权下。
- 原单显示八项终验通过。分别点击「下载 search.py」和「下载 update.py」。两次下载均为 HTTP 200，Chrome 落盘文件的 SHA-256 与原单归档逐项一致。交付 SHA-256 为 `84c29c7fb73895857b009b7914249cb036d188e91ec26d551e6f040ea3ccc350`。
- 修正了普通表单勾选试点后仍显示旧模型组合的问题。Chrome 复测勾选和取消勾选时，试点授权提示与普通模式提示各自正确。这次表单复测只检查提示切换。

[Chrome 验收记录](../../evidence/goal-coordinator/chrome.json)

## 复现入口

在隔离实例中使用已安装的 Autoloop、Python 3 和已登录的 Codex。首次正常对照、故障回归、暂停竞态各运行一条命令。

```bash
python3 -B scripts/live_filename_index.py --formal-source "$HOME/Library/Application Support/Autoloop/workbench/app" --mode control
python3 -B scripts/live_filename_index.py --formal-source "$HOME/Library/Application Support/Autoloop/workbench/app" --mode regression
python3 -B scripts/live_filename_index.py --formal-source "$HOME/Library/Application Support/Autoloop/workbench/app" --mode pause-stale
```

回环代理连不上模型服务的失败尝试留在测试机，后续验收使用直连。首份对照草案每个脚本只有一组样例，现行 V2 因缺对照样例拒绝批准。补齐区分输入和预期后，正常对照通过。复核监听进程后改用实际安装目录。对照首次进入验收时发现已安装应用缺少候选 V2 使用的 Seatbelt 策略，生成器现从仓库补入两份可信依赖。安装源码入口文件及验证器已记录 SHA-256。正式监听进程在各轮复核中保持同一 PID。[源码与依赖摘要](../../evidence/goal-coordinator/installed-source.json)

验收目录保存原工单、隔离 Hub 和归档成果，可按手册备份和校验。[回退步骤](../operations/goal-coordinator-rollback.md)
