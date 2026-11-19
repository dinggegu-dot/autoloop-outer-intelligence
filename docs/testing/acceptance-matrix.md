# 需求与验证

对应[候选 PR #4](https://github.com/dinggegu-dot/autoloop-outer-intelligence/pull/4)。[#1](https://github.com/dinggegu-dot/autoloop-outer-intelligence/issues/1)负责恢复基线、私有仓库和测试入口，[#2](https://github.com/dinggegu-dot/autoloop-outer-intelligence/issues/2)负责接单与双端决定，[#3](https://github.com/dinggegu-dot/autoloop-outer-intelligence/issues/3)负责独立验收和协调。

| 需求 | Issue | 可追踪的验收与证据 |
| --- | --- | --- |
| FR-01 统一接入 | #2 | [事件与选择](../../网页工作台/superagent/test_outer_intake.py)的 duplicate_scope_and_selection；[跨端](../../飞书工作入口/test_outer_channels.py)的 mobile_receive_and_web_approval_share_ticket |
| FR-02 归属 | #2 | [真实五条路由样本](../../evidence/route-samples.jsonl)，准确 4/5；三条未绑定消息均未自动附错。保留一条归属与澄清次序偏差 |
| FR-03 目标草案 | #2 | [原始要求留存和显式替换](../../网页工作台/superagent/test_outer_intake.py)的 provenance_keeps_anchored_requirements_and_requires_explicit_replacement |
| FR-04 最小澄清 | #2 | 同一模块的 clarification_reuses_ticket、selection_preserves_pending_question；[真实样本与局限](evaluation.md) |
| FR-05 版本化目标 | #2 | changed_scope_requires_paused_inner_execution、pending_bound_feedback_prevents_approval，旧授权及原工作区保留 |
| FR-06 验收契约 | #3 | [验收目录与覆盖测试](../../网页工作台/superagent/test_outer_verification.py)覆盖缺少行为证据、人工项和未知约束；[会议工单](../../evidence/meeting/verification.json)有四项运行证据 |
| FR-07 Mandate | #2 | 旧卡、重复同版批准、无签名请求和旧 CLI 控制均有拒收回归；[真实授权工单的产物](../../evidence/meeting/intervals.py) |
| FR-08 三个角色 | #2、#3 | [最小上下文、超时与约束隔离](../../网页工作台/superagent/test_outer_model.py)，[实际协调回执](../../evidence/coordination-repaired.json) |
| FR-09 验收器 | #3 | Python CLI 用真实 Seatbelt 拒绝恒定输出、语法错误与缺文件；静态 HTML 与 JSON 独立检查，未注册行为或主观条件显式留缺口 |
| FR-10 目标级继续 | #3 | [首次拒收](../../evidence/coordination-rejected.json)和[修正后真实修复](../../evidence/coordination-repaired.json)。前序 PASS 在终验退化时重新检查，整体通过才关闭 |
| FR-11 双端交互 | #2 | [真实回环 HTTP、签名 Unix socket 与 SQLite](../../飞书工作入口/test_outer_channels.py)；真实飞书投递与批准进度见[执行记录](../progress/v2-status.md) |
| FR-12 用量与评测 | #2、#3 | [固定样本与真实调用评测](evaluation.md)、[会议工单逐角色用量](../../evidence/meeting/verification.json)。命令行回执提供 tokens 和耗时，账单金额无回执 |

复现入口和限制见[手册](../operations/pilot.md)。提交与 CI 对应关系保存在 PR 及私有仓库 Actions 的同 SHA 运行记录中。
