# Autoloop V2.0 — Outer Intelligence Engineering Design Specification

**文档类型：详细技术设计规格（Engineering Design Specification）**

**适用项目：现有 Codex Autoloop**

**设计状态：目标架构已确定，具体代码映射待仓库审计**

**参考设计：DeepSeek Super-agent Domain / Authorization / Execution**

## 0. 最高设计原则

本次开发采用：

**Event-driven Intelligence + Deterministic Task Engine + Codex Execution**

即：

- 事件触发智能判断，而不是模型持续空转。
- LLM 负责理解、建议、规划和复杂问题判断。
- 程序负责正式状态、权限、调度、并发、幂等与恢复。
- Codex 负责具体开发任务。
- 独立验收负责判断产物是否满足已批准目标。

外层的三个智能角色是逻辑组件，不要求三个独立常驻进程。

本项目不引入 DSH，不重新构建一套独立于现有 Autoloop 的任务数据库或 Scheduler。

### 必须始终成立的系统不变量

**INV-01：单一事实来源**

现有 Autoloop Engine 是 Ticket、授权和执行状态的唯一正式写入者。

**INV-02：模型建议不等于正式决定**

任何模型返回值都必须经过结构、版本、权限与业务规则校验。

**INV-03：草案不等于授权**

Draft Ticket 可以存在，但不得据此启动未经授权的实际开发操作。

**INV-04：执行完成不等于目标完成**

Codex Run、工作包、阶段和最终 Goal 必须分别确认。

**INV-05：验收依据不可随意变化**

执行 Agent 不能单方面降低已批准的验收标准。

**INV-06：每一次状态变更可追溯**

必须关联事件、操作者、依据、版本及提交结果。

**INV-07：旧消息与旧执行不能覆盖新状态**

每个需要修改正式状态的操作必须经过版本检查。

**INV-08：恢复不能重复不可逆副作用**

重试前检查原执行、产物、操作记录及外部系统状态。

---

# 1. 整体运行架构与模块划分

系统采用以下逻辑模块：

| 编号 | 模块 | 职责 | 主要运行形式 |
|---|---|---|---|
| C01 | Event Gateway | 接收与标准化事件 | 确定性程序 |
| C02 | Policy & Routing Gate | 身份、权限、明确归属 | 确定性程序 |
| C03 | Context Builder | 构建智能判断上下文 | 程序 + 查询 |
| C04 | Model Adapter | 统一 LLM 调用 | 基础设施 |
| C05 | Intake Assessor | 智能接单与澄清 | Structured LLM |
| C06 | Goal Contract Manager | 目标草案、版本及来源 | 确定性程序 |
| C07 | Acceptance Designer | 设计验收标准和方法 | 受限 Agent |
| C08 | Acceptance Registry | 可信验证器目录 | 确定性程序 |
| C09 | Approval & Mandate Gate | 正式确认与授权 | 确定性程序 |
| C10 | Goal Coordinator | 复杂跨阶段决策 | 受限 Agent |
| C11 | Execution Handoff | 外层与 Codex 的上下文交接 | 确定性程序 |
| C12 | Verification Engine | 执行可信验收 | 程序 + 独立审查 |
| C13 | State / Event / Outbox | 持久化、原子提交、恢复 | SQLite + 后台任务 |
| C14 | Feishu & Web Presentation | 交互、确认、进度、交付 | 现有应用扩展 |
| C15 | Observability & Development Governance | 日志、指标、GitHub 追踪 | 程序 + GitHub |

以上是逻辑组件，不意味着必须创建 15 个独立服务。

优先采用现有项目的模块化单体架构。只有真实存在进程隔离、部署或扩容需求时，才拆出独立服务。

## 1.1 完整调用链

新需求：

Feishu/Web → C01 → C02 → C03 → C05 → C06 → C07 → C08 → C14 → C09 → Existing Scheduler → Codex

执行结果：

Codex → C11 → C12 → Existing Engine → C10（必要时）→ Existing Scheduler / User / Delivery

所有正式状态变化统一经 Engine。

外层智能模块不得绕过 Engine 直接修改 SQLite 表。

---

# 2. C01 — Event Gateway

## 2.1 核心目的

将飞书、网页、内部执行器和验证器产生的事件转换为统一事件格式，确保后续智能判断能够恢复、去重和追踪。

## 2.2 建议事件契约

`AutoloopEventV1`

| 字段 | 类型 | 说明 |
|---|---|---|
| `event_id` | string | 内部稳定事件编号 |
| `source` | enum | feishu / web / executor / verifier / system |
| `source_event_id` | string | 原系统事件编号 |
| `source_thread_id` | string/null | 原会话、消息串或卡片编号 |
| `ticket_id` | string/null | 已确定关联的工单 |
| `actor_id` | string | 经来源认证的操作者 |
| `actor_type` | enum | human / bot / service |
| `event_type` | enum | message / approval / cancel / result / verification / timer 等 |
| `payload_ref` | string | 原始内容的受控存储引用 |
| `occurred_at` | datetime | 原事件时间 |
| `received_at` | datetime | 系统接收时间 |
| `correlation_id` | string | 跨组件调用追踪编号 |
| `schema_version` | string | 契约版本 |

不要把原始飞书消息 ID 直接作为全局唯一 ID。

使用来源、租户或实例范围、原始事件 ID 构成去重身份。

## 2.3 执行流程

事件到达后：

1. 验证来源真实性及签名或认证信息。
2. 标准化事件。
3. 在 SQLite 事务中写入事件。
4. 根据幂等键检查重复投递。
5. 为待处理事件登记持久化任务。
6. 后台消费者领取事件。
7. 完成处理后写入正式 Decision。
8. 需要回复用户时，通过 Outbox 异步发送。

不能在收到飞书消息后直接调用模型、修改数据库、发送消息，而没有先留下可靠的接收记录。

## 2.4 失败与恢复

重复事件：返回已经处理的结果或保持原状态。

消息格式异常：记录拒绝原因，不进入模型。

存储故障：不假装接单成功。

处理进程重启：从未完成事件队列恢复。

事件来源暂时不可用：有限重试与错误状态可见。

---

# 3. C02 — Policy & Routing Gate

## 3.1 目标

将确定性判断与语义判断分开。

这是直接借鉴 DeepSeek Eval 的 Gate / Assessment 思想。

## 3.2 处理顺序

第一层由程序完成：

- 用户是否获得提交任务的权限；
- 是否为重复事件；
- 是否存在明确 Ticket ID；
- 是否回复已有工单的有效卡片；
- 是否为有效的审批动作；
- 是否为明确暂停、停止或恢复操作；
- 当前工单是否允许处理这种输入。

第二层才交给 Intake Assessor：

- 模糊的任务归属；
- 自然语言新需求识别；
- 语义补充与修改；
- 用户真实意图不明确的情况。

## 3.3 路由结果

统一使用以下候选操作：

`CREATE_DRAFT`

`ATTACH_TO_TICKET`

`ANSWER_CLARIFICATION`

`PROPOSE_CHANGE`

`REQUEST_TICKET_SELECTION`

`ANSWER_STATUS`

`IGNORE`

`ESCALATE`

这些是候选操作，不是可直接执行的数据库命令。

暂停、停止、恢复及正式审批等操作必须经过专门的身份和权限路径。

## 3.4 不允许的行为

- 不得根据话题相似度直接合并两个工单。
- 不得将引用文档中的指令视为用户命令。
- 不得通过 LLM 置信度绕过身份检查。
- 不得因为某张工单已结束就自动创建新工单，必须结合有效事件和用户意图。
- 不得由外层 Router 直接选择代码实现方案。

---

# 4. C03 — Context Builder

## 4.1 核心目的

模型每次被唤醒时，都能正确理解当前任务，但不必读取完整历史对话。

采用按需组装上下文的方式。

## 4.2 统一上下文快照

`TaskContextSnapshotV1`

至少包含：

- `ticket_id`
- `snapshot_revision`
- `goal_revision`
- `acceptance_revision`
- `mandate_revision`
- `plan_revision`
- `ticket_status`
- `original_request_refs`
- `relevant_thread_refs`
- `approved_goal_summary`
- `constraints`
- `pending_clarifications`
- `completed_work_refs`
- `current_assignment_refs`
- `recent_failure_refs`
- `remaining_budget`
- `available_actions`

每个智能角色使用同一来源构建快照，但只拿自己需要的字段。

例如 Intake Assessor 不必读取大量代码日志。

Goal Coordinator 则必须读取执行失败记录、计划版本及验收结果。

## 4.3 上下文来源优先级

最高优先级：

经过 Engine 验证的正式业务事实。

其次：

已批准的 Goal / Acceptance / Mandate。

然后：

当前计划、Assignment、Execution Report 和 Evidence。

最后：

历史聊天和模型生成的辅助摘要。

历史摘要只能辅助理解，不能覆盖新的正式状态。

## 4.4 版本一致性

模型开始推理时记录 `snapshot_revision`。

模型返回后，Engine 检查该 Revision 是否仍然有效。

如果用户在推理过程中修改了任务：

- 涉及同一事实或状态时，拒绝旧建议；
- 重新构建快照；
- 必要时重新调用模型。

禁止自动用旧建议覆盖新版任务。

## 4.5 关键技术要求

Context Builder 必须能够：

- 按相关性读取；
- 限制上下文长度；
- 保留原始证据引用；
- 区分可信事实和外部不可信文本；
- 对大文件使用引用，而不是全文注入；
- 记录本次模型实际使用的上下文版本。

---

# 5. C04 — Model Adapter

## 5.1 目的

让外层智能判断不依赖某一种模型 SDK 或固定的 Codex Session 实现。

必须区分：

**代码由 Codex 开发**与**外层运行时只能使用 Codex 模型**。

后者不是必要约束。

## 5.2 统一调用接口

建议抽象为：

`invoke_structured(role, context, output_schema, policy)`

`invoke_bounded_agent(role, context, toolset, output_schema, policy)`

两者都需要返回：

- Invocation ID；
- 输出内容或结构化结果；
- 模型配置；
- 输入快照 ID；
- Prompt 版本；
- Token / 用量；
- 执行耗时；
- 失败类别；
- 是否被取消。

## 5.3 调用方式

Intake Assessor 默认采用一次结构化模型调用。

Acceptance Designer 和 Goal Coordinator 在需要查询事实时，可以采用有限步 Tool Calling。

不要求每个角色启动一个长期 Codex Session。

## 5.4 模型和执行资源

模型选择通过配置管理。

建议逻辑为：

| 场景 | 策略 |
|---|---|
| 明确事件归属 | 程序，不调用 LLM |
| 普通语义研判 | 较低成本的结构化模型调用 |
| 验收方案设计 | 中等至较高推理能力 |
| 复杂重新规划 | 高推理能力 |
| 代码实现 | 现有 Codex Executor |
| 确定性验收 | 程序 |

具体模型名称、超时与预算使用部署配置，而不是写死在领域代码中。

## 5.5 模型失败策略

模型无响应：有限重试。

返回不符合 Schema：允许一次受控格式修复。

引用不存在的工单或证据：拒绝结果。

调用超预算：中断自动判断，保存待处理状态。

多次失败：转为 `UNAVAILABLE` 或 `NEEDS_ATTENTION`。

不得通过无限重试掩盖系统失败。

---

# 6. C05 — Intake Assessor

## 6.1 运行形式

默认 Structured LLM Call。

只有确实需要检索多个工单或历史消息时，才启用只读 Tool Calling。

## 6.2 输入

- 标准化 Event；
- 当前来源 Thread；
- 用户有权访问的候选 Ticket；
- 当前待回答的澄清问题；
- 相关已批准任务摘要；
- 当前 Policy。

## 6.3 输出 Schema

`IntakeProposalV1`

至少包含：

- `schema_version`
- `event_id`
- `input_snapshot_revision`
- `intent`
- `relation_type`
- `candidate_ticket_ids`
- `relation_evidence_refs`
- `goal_draft`
- `explicit_requirements`
- `assumptions`
- `critical_ambiguities`
- `proposed_question`
- `suggested_action`
- `reason`

其中每项需求和假设必须保留来源分类：

`USER_EXPLICIT`

`USER_CONFIRMED`

`CONTEXT_DERIVED`

`MODEL_PROPOSED`

模型不能把 `MODEL_PROPOSED` 自动写成 `USER_CONFIRMED`。

## 6.4 关键歧义判断算法

每项歧义应判断：

- 是否影响核心交付物；
- 是否影响关键验收条件；
- 是否影响权限或重大成本；
- 是否存在安全、可逆的默认选择；
- 错误默认的返工成本；
- 当前能否继续进行不依赖该答案的工作。

如果具有重大影响且无法安全默认，返回一个需要用户确认的问题。

否则记录默认假设，继续整理目标。

## 6.5 Clarification Protocol

澄清问题必须持久化，而不是只留在聊天内容中。

记录：

- `question_id`
- `ticket_id`
- `goal_revision`
- `question_text`
- `options`
- `reason`
- `blocking_scope`
- `answer_status`
- `answer_event_id`
- `answered_by`
- `answered_at`

用户回复必须关联具体问题及有效工单版本。

如果用户同时回复多项内容，系统可以提取并更新相关字段，但不得重复追问已经明确回答的问题。

## 6.6 最终验收

模型能够将陌生自然语言请求转化为可理解的 Goal Draft。

同一工单的澄清不会生成重复 Ticket。

存在多个合理工单候选时不会错误自动归属。

没有关键歧义时不为了形式完整而反复询问用户。

---

# 7. C06 — Goal Contract Manager

## 7.1 目的

让模糊自然语言变成可追踪的正式产品目标。

## 7.2 目标契约结构

`GoalContractRevision`

至少记录：

- `ticket_id`
- `revision`
- `goal_statement`
- `deliverables`
- `required_capabilities`
- `nonfunctional_requirements`
- `constraints`
- `assumptions`
- `out_of_scope`
- `source_refs`
- `created_by`
- `created_at`
- `status`

建议状态：

`DRAFT`

`READY_FOR_APPROVAL`

`APPROVED`

`SUPERSEDED`

正式批准后，该版本不可覆盖修改。

后续变更创建新 Revision。

## 7.3 草案 Ticket

允许一个 Ticket 在尚未授权时保存需求草案。

但要区分：

`Ticket Created`

与：

`Execution Authorized`

这不是一个事件。

实际执行必须要求有效 Mandate。

## 7.4 变更管理

模型或用户提出修改后，系统生成新 Goal Revision，并形成差异：

- 增加了什么；
- 删除了什么；
- 哪些属于重要功能变化；
- 哪些只属于普通实现细节；
- 是否影响验收；
- 是否影响授权和预算；
- 现有执行成果是否仍然有效。

对于超出授权的变化，不允许继续执行新范围。

---

# 8. C07 — Acceptance Designer

## 8.1 运行形式

Bounded Tool-using Agent（受限工具调用智能体）。

不提供任意 Shell、数据库写入或批准权限。

## 8.2 输入

- Goal Contract；
- 当前需求列表；
- 已有 Acceptance Revision；
- 验收器目录；
- 验证环境能力；
- 已批准限制；
- 必要的用户澄清结果。

## 8.3 允许的只读工具

`read_goal_contract`

`list_available_verifiers`

`inspect_verifier_capability`

`inspect_verification_environment`

`read_prior_acceptance`

`read_relevant_requirement_refs`

工具调用必须受到数量、执行时间和资源限制。

## 8.4 输出

`AcceptanceProposalV1`

包含：

- `ticket_id`
- `goal_revision`
- `artifact_types`
- `criteria`
- `verifier_bindings`
- `test_input_requirements`
- `evidence_requirements`
- `verification_gaps`
- `human_review_requirements`
- `coverage_assessment`
- `source_refs`

## 8.5 每条验收条件的字段

建议：

- `criterion_id`
- `requirement_ids`
- `description`
- `required`
- `verification_type`
- `verifier_id`
- `verifier_version`
- `test_spec`
- `expected_result`
- `evidence_required`
- `human_review_required`
- `source_refs`

验证类型：

`DETERMINISTIC`

`MODEL_REVIEW`

`HUMAN_REVIEW`

`HYBRID`

## 8.6 验收条件设计规则

每条用户必要需求应关联至少一条验收条件。

每条验收条件应能说明：

“证明了什么？”

“由什么方法证明？”

“哪些情况不能证明？”

对于无法充分自动验证的目标，必须明确人工验收安排或标记验证缺口。

例如：

用户要求“DJ 音乐过渡自然”。

不能直接将“成功输出 MP3 文件”作为唯一验收标准。

应同时检查实际音频处理能力、基础连续性与必要的主观试听反馈。

## 8.7 可信性控制

Acceptance Designer 可以建议新测试，但不能：

- 修改已批准标准；
- 降低必需条件；
- 自动批准新增验证器；
- 自己给自己生成通过结果；
- 跳过不可验证的必要需求。

生成的 Acceptance Proposal 由程序检查覆盖关系、真实验证器、环境能力和版本。

必要时使用独立模型审查验收条件与原始需求的语义一致性。

## 8.8 最终输出状态

`READY_FOR_APPROVAL`

`NEEDS_CLARIFICATION`

`VERIFICATION_GAP`

`FAILED`

只有满足批准要求的方案，才能进入正式确认。

---

# 9. C08 — Acceptance Registry

## 9.1 定位

可信验收器目录不等于一组由 LLM 随意生成的测试脚本。

它应当拥有明确的注册、执行、结果与证据协议。

## 9.2 Verifier Definition

每个验证器至少声明：

- `verifier_id`
- `version`
- `artifact_types`
- `capabilities`
- `required_inputs`
- `supported_environments`
- `permission_requirements`
- `runner`
- `result_schema`
- `evidence_schema`
- `resource_limits`
- `trust_status`

## 9.3 初始验收能力

优先复用现有测试能力。

建议扩展：

| 类型 | 核心验收 |
|---|---|
| CLI / Python | 命令执行、输入输出、异常处理 |
| Web App | 构建、启动、核心 UI 操作 |
| Data | 数据格式、完整性、准确性 |
| File Artifact | 文件完整性、格式及可打开性 |
| API | 合同、响应、错误行为 |
| Model Review | 语义质量与需求覆盖审查 |
| Human Review | 主观质量、重要业务判断 |

音频和桌面验收可以按实际需求增加，但接口设计必须支持扩展。

## 9.4 验证器运行安全

验证器应在隔离或受限执行环境中运行。

不得自动以用户的高权限环境运行未知测试脚本。

区分：

- 用户已批准的验收要求；
- 执行 Agent 编写的测试；
- 系统信任的验证器；
- 最终提交的待验证产物。

执行 Agent 提交的测试只能作为候选验证材料；正式验收必须遵守已经批准的预期与可信执行策略。

## 9.5 版本绑定

每条验证结果必须绑定：

- Artifact ID；
- Artifact SHA-256；
- Git Commit 或不可变版本；
- Acceptance Revision；
- Verifier ID / Version；
- Test Fixture Version；
- Environment；
- Result；
- Evidence Ref。

如果产物改变，旧结果不得自动视为新版通过。

---

# 10. C09 — Approval & Mandate Gate

## 10.1 核心目的

让用户一次确认必要目标、验收、执行范围及资源预算，而不是逐阶段反复批准普通技术细节。

## 10.2 批准对象

一次正式批准至少绑定：

- Ticket ID；
- Goal Revision；
- Acceptance Revision；
- 授权的工作目录；
- 允许的操作范围；
- 模型或执行资源预算；
- 禁止操作；
- 批准人；
- 批准事件；
- 时间与版本。

## 10.3 Approval Command

建议后端接收：

`approve_goal_contract(ticket_id, goal_revision, acceptance_revision, expected_ticket_revision, approval_event_id)`

调用方身份由服务器端认证信息确定，不能依赖模型或请求正文声称的身份。

必须核查当前 Ticket 版本以及是否存在有效冲突。

## 10.4 Mandate

有效 Mandate 应绑定实际获准的目标与约束。

新授权版本不得覆写旧历史。

范围变更时，旧 Assignment 的处理应依据影响分析：

- 不受影响的只读或已验证成果可以保留；
- 与新范围冲突的执行应受控取消或隔离；
- 不能仅靠 Prompt 告诉旧 Worker“注意新要求”。

## 10.5 飞书确认卡

确认卡应展示：

- 目标摘要；
- 必需交付；
- 验收标准；
- Agent 可以自主决定的事项；
- 禁止操作；
- 预算与重要限制；
- 未解决的人工验收条件。

审批事件必须带原卡和契约版本引用。

旧卡、重复点击、无权限用户和已撤销授权均不得产生新执行权限。

---

# 11. C10 — Goal Coordinator

## 11.1 定位

负责复杂目标的持续管理，不负责普通技术实施。

采用事件驱动的受限 Agent。

它不是第二个 Codex Lead。

## 11.2 启动条件

优先由确定性规则触发。

例如：

- 总体目标验收失败；
- 同类失败超过设定尝试预算；
- 计划无法继续；
- 工作包结果之间发生冲突；
- 原技术路线被证明不可行；
- 用户提出重大目标变化；
- 需要跨阶段重新规划。

普通测试失败由 Inner Loop 优先处理。

明确下一阶段已就绪时由 Scheduler 自动派工。

## 11.3 输入

`GoalCoordinationContextV1`

包含：

- 当前有效 Goal / Acceptance / Mandate；
- 当前 Plan Revision；
- 已完成工作及证据；
- 失败尝试摘要；
- 未通过的具体 Criteria；
- 现有产物版本；
- 剩余资源预算；
- 当前授权内可选操作；
- 用户最新有效反馈。

## 11.4 只读工具

`get_ticket_snapshot`

`get_plan_history`

`get_execution_summary`

`get_evaluation_results`

`get_artifact_metadata`

`get_remaining_budget`

`list_allowed_actions`

不提供直接修改正式 Ticket 的权限。

## 11.5 输出

`GoalCoordinationProposalV1`

必须返回一个主要建议：

`CONTINUE`

`REPAIR`

`REPLAN`

`REQUEST_APPROVAL`

`BLOCKED`

并包含：

- 选择原因；
- 相关证据；
- 需要改变的计划部分；
- 对现有成果的影响；
- 是否涉及范围变化；
- 是否需要用户批准；
- 剩余风险。

不能返回“已完成”作为没有证据支持的自我判断。

最终完成判断由 Verification Engine 与 Engine 完成。

## 11.6 重新规划机制

Goal Coordinator 不应直接生成大量工作包并覆盖当前 Plan。

它应先提出：

`Replan Proposal`

Engine 检查：

- 当前是否允许重新规划；
- 是否处于暂停或停止状态；
- 是否仍有剩余预算；
- 是否存在与当前有效 Mandate 冲突的修改；
- 是否需要取消旧 Assignment。

通过后再调用现有 Codex Planner 形成新版正式计划。

新版计划必须记录前驱版本、修改原因、受影响工作包及保留成果。

## 11.7 防止无限循环

必须具备：

- 每类失败的重试上限；
- 总预算限制；
- 无实质进展检测；
- 重复方案识别；
- 等待人工决策的退出路径。

不得因为模型连续建议 `REPAIR` 就无限执行。

---

# 12. C11 — Execution Handoff

## 12.1 目的

保证 Outer Loop 和现有 Codex Executor 之间存在稳定、可核查的上下文交接协议。

## 12.2 Execution Contract

每次 Assignment 启动时必须绑定：

- Ticket ID；
- Assignment ID；
- Run ID；
- Goal Revision；
- Acceptance Revision；
- Mandate Revision；
- Plan Revision；
- 当前工作包；
- 工作目录和代码版本；
- 本次任务成功条件；
- 可调用工具和权限；
- 资源预算；
- 已有成果与依赖；
- 必要的历史失败证据。

## 12.3 Execution Report

Codex 执行完成时应返回：

- 实际完成工作；
- 修改的代码和文件引用；
- Git Commit 或工作区快照；
- 自动测试结果；
- 未解决问题；
- 与验收条件的关系；
- 推荐下一步；
- 是否存在未确认的外部副作用。

这些信息是事实和建议的组合，不能未经验证就直接更新为“已验收”。

## 12.4 Session 恢复

如果现有 Codex 执行器支持可靠 Session Resume，可以使用。

但正式任务连续性不能只依赖 Session History。

恢复时必须先检查：

- SQLite Run 记录；
- 实际进程；
- 工作目录；
- Git 状态；
- 已生成产物；
- 已完成外部操作；
- 当前授权与计划版本。

无法恢复原 Session 时，使用正式 Execution Contract 和已有产物启动新执行。

---

# 13. C12 — Verification Engine

## 13.1 验收层级

系统必须具有四层判断：

| 层级 | 检查对象 |
|---|---|
| Run | 本次执行是否正常结束 |
| Work Package | 当前工作包是否满足条件 |
| Stage | 阶段整体要求是否满足 |
| Goal | 最终目标是否完全满足 |

Run 成功不能直接推导 Work Package 成功。

Work Package 全部通过也不能直接推导最终 Goal 通过。

## 13.2 验收结果

每条 Criterion 采用：

`PASS`

`FAIL`

`INCONCLUSIVE`

`NOT_RUN`

`ERROR`

`PASS` 必须具备有效证据。

`INCONCLUSIVE` 表示证据不足，不能算通过。

`ERROR` 表示验证器执行异常，不能算交付失败或通过，需按实际情况重试或升级。

## 13.3 最终完成条件

只有当前有效 Acceptance Revision 中的全部必需项获得有效 `PASS`，且不存在必要人工验收待确认、授权异常或版本冲突时，才允许正式标记目标已验证完成。

允许阶段性交付，但必须区分：

`PARTIAL_DELIVERY`

`AWAITING_REVIEW`

`VERIFIED_COMPLETE`

## 13.4 防止假通过

必须覆盖下列情况：

- 文件存在但不能运行；
- README 宣称有功能但代码没有；
- 程序启动但核心交互失败；
- 测试只覆盖简单输入；
- 交付版本与验收版本不一致；
- 执行 Agent 修改预期测试值；
- 某项必需条件没有验证器；
- 主观要求未经用户确认。

## 13.5 目标级反馈

验收失败后生成结构化 Evaluation Result。

根据具体情况：

- 明确局部 Bug → Inner Loop 修复；
- 明确剩余阶段 → Scheduler 继续；
- 复杂跨阶段问题 → Goal Coordinator；
- 缺少授权 → 请求用户；
- 无法验证 → 保留待验证状态。

---

# 14. C13 — SQLite State、事件和 Outbox

## 14.1 数据设计

优先复用现有数据库。

请先检查实际表结构，不得无理由创建重复表。

逻辑上至少需要支持：

- Events / Triggers；
- Threads / Subscriptions；
- Tickets；
- Goal Contract Revisions；
- Acceptance Contract Revisions；
- Mandates；
- Clarifications；
- Intelligence Invocations；
- Intelligence Proposals；
- Decisions；
- Assignments / Runs；
- Artifacts；
- Verification Results；
- Outbox Deliveries。

## 14.2 正式写入机制

Engine 是唯一正式状态写入者。

建议每个状态变更：

1. 检查身份与权限；
2. 检查预期版本；
3. 检查业务状态；
4. 检查幂等性；
5. 在一个 SQLite 事务中提交正式变化；
6. 同事务登记需要向外部发送的消息或执行意图；
7. 事务提交后由后台消费者执行副作用。

不要让 LLM 调用或飞书网络请求占用数据库事务。

## 14.3 幂等与重试

幂等键必须覆盖：

- 接收到的消息；
- 用户审批；
- 正式任务创建；
- Assignment 启动；
- Run 领取；
- 外部消息投递；
- 可重复触发的外部副作用。

恢复时优先查找已有结果，不能直接重新执行。

## 14.4 并发控制

同一 Ticket 的正式变更必须序列化或使用可靠版本比较。

如果请求的预期 Revision 与当前版本不一致：

拒绝旧变更，返回冲突和当前状态。

不得通过静默覆盖解决冲突。

---

# 15. C14 — Feishu 与 Web 产品交互

## 15.1 核心原则

**用户管理目标，不管理每一次普通模型执行。**

保留现有双端工作台。

## 15.2 单张工单的主要视图

建议展示：

**Goal**

当前确认的目标与交付物。

**Scope**

当前允许的操作范围与预算。

**Progress**

实际已完成工作、验证状态及下一步。

**Attention**

真正需要用户确认的问题。

**Delivery**

已交付版本、文件、测试与验收证据。

## 15.3 飞书卡片

建议至少支持：

- 关键澄清；
- 目标与验收确认；
- 授权变化；
- 暂停；
- 续办；
- 查看进度；
- 查看交付与验收结果。

不要求在聊天中展示所有内部 Agent 思考过程。

优先展示用户需要行动或能够直接使用的内容。

## 15.4 双端一致性

每一个用户操作都通过同一业务 API 提交。

飞书和 Web 不分别维护一套状态。

旧卡操作必须进行版本检查。

如果卡片过期，应展示当前有效状态，而不是错误执行旧操作。

---

# 16. C15 — Observability 与 GitHub 开发治理

## 16.1 运行时观测

统一 `correlation_id` 贯穿：

Event → LLM Invocation → Proposal → Decision → Assignment → Run → Evidence。

支持从一次用户消息追踪到最终决定及交付版本。

关键指标：

- LLM 路由准确率；
- 错误自动归属率；
- 必要澄清次数；
- 不必要澄清次数；
- 验收覆盖率；
- 不合格交付假通过率；
- 跨阶段自动继续次数；
- 用户介入次数；
- 模型 Token 与执行成本；
- 恢复成功率；
- 重复外部操作次数。

## 16.2 GitHub 开发记录

所有正式开发必须关联：

**PRD Requirement → Engineering Issue → Code / Commit → Pull Request → Test → Evidence**

GitHub 负责版本化开发记录。

SQLite 继续负责业务运行状态。

绝对不能把运行数据库、飞书消息、私人资产或密钥作为 GitHub 开发记录直接上传。

## 16.3 开发文档

必须创建或更新：

`docs/product/PRD-v2-outer-loop.md`

`docs/architecture/outer-intelligence.md`

`docs/architecture/data-contracts.md`

`docs/architecture/state-machines.md`

`docs/architecture/security-and-authorization.md`

`docs/architecture/acceptance-system.md`

`docs/architecture/recovery-and-idempotency.md`

`docs/plans/v2-implementation-plan.md`

`docs/testing/outer-loop-evaluation.md`

`docs/decisions/ADR-INDEX.md`

`docs/progress/v2-status.md`

具体路径应先检查已有仓库文档结构，避免重复。

## 16.4 文档同步规则

代码变更影响接口、Schema、状态或规则时，必须在同一开发批次更新对应文档和测试。

每项主要需求必须能够追踪真实 GitHub Issue、PR 和测试。

不得创造虚假的 GitHub 编号或测试证据。

CI 应检查能够自动检查的文档、Schema 和测试一致性。

---

# 17. 三个智能角色的 Prompt 设计原则

## 17.1 Intake Assessor

角色说明：

你只负责理解用户消息、识别任务归属、生成目标草案和提出必要澄清。

你不能批准工作、修改数据库、扩大权限或执行开发。

你必须区分用户明确提出的事实与自己的假设。

对于多个可能关联的工单，不能凭主题相似度自行选择。

只返回符合 IntakeProposal Schema 的结果。

## 17.2 Acceptance Designer

角色说明：

你负责将正式目标转换为可验证的验收条件。

你应当查询真实验收器能力，明确每项条件的证据与验证限制。

你不能为了让任务容易完成而降低用户标准。

如果系统缺少必要验收能力，必须明确报告验证缺口。

只返回符合 AcceptanceProposal Schema 的结果。

## 17.3 Goal Coordinator

角色说明：

你负责在总体目标未达标、执行遇到跨阶段障碍或需要重新规划时，提出合理的下一步建议。

你不能直接更改授权、执行高风险操作、绕过暂停状态，或无证据地宣布目标完成。

优先复用已完成且通过验收的成果，避免无意义重复执行。

只返回符合 GoalCoordinationProposal Schema 的结果。

以上 Prompt 应当版本化管理，并通过测试样本评估，不能仅凭单次演示确认有效。

---

# 18. 端到端技术测试矩阵

必须有自动化测试与真实系统测试。

| 场景 | 必须证明 |
|---|---|
| 同一飞书事件重复投递 | 不重复创建工单 |
| 模糊新目标 | 形成可理解的 Goal Draft |
| 回复原工单卡片 | 正确关联原 Ticket |
| 同时存在两个相似项目 | 不错误合并 |
| 用户回答澄清问题 | 正确更新同一 Goal Draft |
| 普通低风险细节 | 不反复询问 |
| 重大交付歧义 | 请求必要确认 |
| 模型建议扩大授权 | Engine 拒绝直接执行 |
| 用户批准旧版本 | 版本冲突，拒绝 |
| 模型引用不存在的工单 | 拒绝 Proposal |
| 模型返回非法结构 | 受控修复或标记失败 |
| 所有工作包通过但 Goal 失败 | 不宣布完成 |
| 只有文件但不能运行 | 验收失败 |
| 缺少必要验证器 | 标记验证缺口 |
| 交付修改后引用旧测试 | 拒绝旧证据 |
| 反复技术失败 | 触发 Goal Coordinator |
| Goal Coordinator 建议越权 | 拒绝建议 |
| 用户暂停后收到完成回执 | 记录结果但不自动继续 |
| 协调器重启 | 从持久状态恢复 |
| 回执丢失 | 先对账，再决定重试 |
| 飞书发送失败 | Outbox 保留待投递 |
| 多个用户同时操作工单 | 版本与身份检查正确 |
| 新版功能开启 | 原有执行路径保持可用 |

所有已有 Autoloop 实测项目都必须回归。

真实 Codex 测试、离线协议替身和纯单元测试必须分别标注，不得混淆。

---

# 19. 分阶段实施计划与依赖

这属于一个完整的 Outer Loop V2.0 建设任务。

允许按依赖实施，不允许在完成局部 Demo 后自行停止整个目标。

| 阶段 | 技术内容 | 交付门槛 |
|---|---|---|
| P0 | 代码审计、恢复基线、GitHub 安全接入 | 现有系统可恢复 |
| P1 | C01、C02、C13 基础事件和规则 | 接收、去重、状态可靠 |
| P2 | C03、C04 智能基础设施 | 模型建议可校验和追踪 |
| P3 | C05、C06 智能接单与目标草案 | 陌生需求可靠整理 |
| P4 | C07、C08 验收设计与目录 | 验收方案可信 |
| P5 | C09、C14 审批与双端交互 | 正式授权及版本一致 |
| P6 | C10、C11、C12 目标协调、交接和验证 | 自动接续且不假通过 |
| P7 | C15 与全面回归、GitHub 对齐 | 有可核查的完整交付证据 |

P0 审计后，可以调整模块实施顺序，但必须记录技术理由和依赖变化。

---

# 20. 开发执行和质量要求

## 20.1 开发前

先审计真实代码与数据库结构。

建立安全恢复基线。

核对现有 GitHub Remote、分支、未提交工作、数据库和运行环境。

先形成最新 PRD、数据模型、接口规格和测试计划。

必须包含“质疑与澄清”章节，主动说明过度设计、重复实现、权限缺口和潜在冲突。

## 20.2 开发期间

Codex 负责完整代码开发。

重要架构问题可使用高思考强度模型；普通模块按实际复杂度使用合适配置。

保持现有 Autoloop 运行路线不受破坏，新功能通过隔离测试环境与 Feature Flag（功能开关）逐步上线。

每个批次更新：

- 对应 Issue；
- 源码与测试；
- PRD 或架构文档；
- 开发进度；
- 实际测试记录；
- GitHub Commit / PR。

不得未经用户授权公开仓库或自动合并受保护主分支。

不得上传敏感数据及密钥。

## 20.3 代码质量

遵守现有项目技术栈及命名规范。

关键业务逻辑、权限机制、状态变更和恢复代码需要必要的中文解释注释。

数据迁移必须可测试，并有恢复或兼容方案。

所有新增接口必须有输入验证、错误类型、权限检查与自动化测试。

不为形式上的“架构先进”引入不必要的微服务或消息队列。

---

# 21. 最终 Definition of Done

本次开发完成必须证明：

**智能性**

用户一句自然语言请求可以建立合理目标草案；模型可以识别关键歧义并提出必要问题；复杂验收可以选择正确方法；跨阶段问题可以提出合理行动建议。

**可靠性**

模型失败、事件重复、消息乱序、旧卡审批、协调器重启、回执丢失和用户暂停均不会破坏正式状态。

**自主性**

普通开发步骤不需要用户反复发送“继续”；真正需要用户参与的操作能够被明确识别。

**质量**

运行成功、工作包成功和最终目标成功严格区分；无证据不能标记验收通过。

**开发治理**

所有正式代码、PRD、架构、工程计划、测试及脱敏验收摘要都已安全提交并与 GitHub 对齐，可以定位每次修改的原因和恢复版本。

**兼容性**

此前已验证的 Autoloop 串行运行、并行、局部返工、恢复、跨端控制和容量管理能力，没有未披露的回归。

---

# 22. 对 Codex 的最终执行指示

本技术规格与此前《Autoloop V2.0 — Intelligent Outer Loop & GitHub-Native Development》主任务书共同构成本次完整开发任务。

此前文档定义产品目标、GitHub 管理和交付要求。

本文件定义具体组件、接口、状态、权限、智能判断和验收技术方案。

如出现冲突：

- 用户已明确的目标、安全和授权要求优先；
- 真实代码及测试事实必须经过核查；
- 不得静默修改已确认的核心产品目标；
- 对不可兼容之处提出技术异议、替代方案与迁移成本；
- 在得到必要决定后更新 PRD 和 Architecture Decision Record。

请先完成代码审计及详细设计映射，再进行完整实施。

**不要仅创建接口占位、模拟结果或文档，就宣称 V2.0 完成。**

**不要完成一个局部开发切片就停止整个任务。**

**不要让外层 LLM 变成第二个拥有数据库控制权的任务管理系统。**

最终交付必须是：

**完整可运行的 Outer Intelligence V2.0 + 保留原有 Autoloop 能力 + GitHub 全链路开发记录 + 真实测试证据 + 可回滚的软件版本。**
