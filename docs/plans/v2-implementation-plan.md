# 实施依赖与放行条件

| 批次 | 工程任务 | 输入与放行条件 |
| --- | --- | --- |
| 0–1 | [V2-01](https://github.com/dinggegu-dot/autoloop-outer-intelligence/issues/1) | 取证、在线备份、导入前审查、基线回归和可追踪分支 |
| 2–4 | [V2-02](https://github.com/dinggegu-dot/autoloop-outer-intelligence/issues/2) | 固化来源和版本，以受限模型形成建议，澄清与批准通过双端命令门，随后衔接同单派工 |
| 5–6 | [V2-03](https://github.com/dinggegu-dot/autoloop-outer-intelligence/issues/3) | 注册的验收器、版本化证据与有限协调，失败产物不能关闭总目标 |
| 7–8 | 三项任务联合验收 | A 自然语言，B 坏产物，C 跨阶段，D 故障，E 回归，F 质量。真实模型与真实手机各自记录，代码、验证环境和证据同步至 PR |

协议衔接先约定，再在独立工作目录实施。集成仅由主工作区写入者负责。测试与证据分别标注夹具、真实服务、真实模型和本人设备。每次放行核对当前提交和未解决依赖。

## 交付状态

- 治理批次已建立私有仓库、分支、PR、可恢复基线、静态检查及有界 CI。运行手册覆盖旧服务保留、候选启动及按原路径恢复。
- 接单批次已接入真实网页会话与飞书私聊、模型提案、来源追踪、澄清、版本冲突检验和同版授权。真实五条路由评测保留一条偏差。
- 验收批次已验证非 CSV CLI 交付、跨阶段回归发现与实际 Codex 修复。交互式网页、网络 API 及主观指标仍列入验证缺口。
- 联合验收的结果与限制分别见[执行记录](../progress/v2-status.md)、[质量评测](../testing/evaluation.md)及[五单容量实测](../../evidence/capacity-regression.md)。生产切换与合并由仓库维护者决定。
