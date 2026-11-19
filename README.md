# Autoloop V2 智能接单与目标执行

在本机看板或本人飞书私聊提交目标，查看模型拟定的目标和验收条件。批准当前版本后，隔离 Hub 调度 Codex 执行，工作台根据归档文件的独立检查结果展示交付。

## 本机运行

在 macOS 上准备 Python 3.11、Node、已有 Autoloop 中台和可用的 Codex CLI 登录。按[隔离试点运行手册](docs/operations/pilot.md)启动 Hub、Worker、协调器和网页入口。仓库保留程序、治理文档和合成测试，运行数据库与真实凭据在本机管理。

从仓库根目录运行完整的无账户回归：

```bash
GOAL_CHECKS_REAL_SEATBELT=1 python3 -B scripts/check.py
```

真实模型验收使用独立临时实例和既有 Codex 账户：

```bash
pilot=$(mktemp -d /private/tmp/autoloop-v2-live-meeting-XXXXXXXX)
bash 网页工作台/superagent/isolated_goal_hub.sh "$pilot" 43672 -- python3 -B scripts/live_acceptance.py
```

## 工程与验收

- [产品任务与完成定义](docs/product/PRD-v2-outer-loop.md)和[架构设计规格](docs/architecture/outer-intelligence.md)
- [源码审计、迁移与恢复边界](docs/architecture/system-overview.md)和[架构取舍](docs/decisions/ADR-INDEX.md)
- [实施计划](docs/plans/v2-implementation-plan.md)、[进度与未决项](docs/progress/v2-status.md)及[需求验收矩阵](docs/testing/acceptance-matrix.md)
- [离线评测与样本范围](docs/testing/evaluation.md)和[候选版 PR](https://github.com/dinggegu-dot/autoloop-outer-intelligence/pull/4)
