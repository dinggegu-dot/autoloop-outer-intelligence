# Phase F 可重复评测

2026-11-19 在独立克隆对候选提交 `c4584d2ad6e87e2d62577c7555256d1571c6957c` 运行本机测试。环境为 macOS 26.6.2、Darwin 25.6.0、arm64、Python 3.9.6 和 Node.js 22.23.2。全程使用合成输入和临时数据库。运行了真实本机文件、SQLite、HTTP 回环、Unix socket、子进程及 Seatbelt。飞书传输替换为夹具，模型输出使用协议替身，本次未发生模型调用或账户费用。

## 运行结果

| 测试组 | 通过／总数 | 执行方式 |
| --- | ---: | --- |
| 看板 Python 回归 | 90/90 | 本机单元和集成测试 |
| 目标引擎与 Superagent Python 回归 | 89/89 | 本机运行，含 3 项真实 Seatbelt 专用用例 |
| 飞书入口 Python 回归 | 40/40 | 本机运行，含 2 项双端 HTTP 与私有 socket 联动测试 |
| 旅程及展示 JavaScript 回归 | 18/18 | Node 测试 |
| 合计 | 237/237 | 本轮无失败，无跳过 |

仓库检查通过，检查程序报告 7 个可达提交、108 个文件版本、12 项需求映射。JavaScript 语法检查和 Git 差异检查也通过。上述总数按测试方法或 Node 测试项计。方法内的多次断言、协议重放和表格子场景未计入总数。

## 固定样本的结果

以下分母均来自当前仓库的有限合成样本。各行可能引用同一测试方法。

| 要验证的行为 | 分母与结果 | 对应测试 |
| --- | --- | --- |
| 入口状态、事件与权限门 | 11/11 个入口测试方法通过。覆盖同源重投、跨租户同号、未授权选单、原卡及旧卡、草案变更、旧批准和有界恢复 | [入口测试](../../网页工作台/superagent/test_outer_intake.py) |
| 选单与澄清状态 | 2/2 个选择场景保持预期的工单状态。其一在两个候选间显式选择，其一保留当前问项且不再次调用替身。另有 1/1 个明确设为歧义的场景在原单澄清，未多建工单 | [入口测试](../../网页工作台/superagent/test_outer_intake.py) |
| 模型输出的结构和权限 | 2/2 个适配器测试方法通过，其中覆盖金丝雀上下文、禁用工具参数、无效输出和超时。1/1 个接单用例拒绝替身返回的直接授权建议，同一测试也拒绝验收缺口工单 | [模型适配器测试](../../网页工作台/superagent/test_outer_model.py)、[入口测试](../../网页工作台/superagent/test_outer_intake.py) |
| 双端正式状态一致 | 2/2 个本机联动测试通过。合成飞书事件经签名私有 socket 接单，实际网页会话批准同一工单并去重。另一个用例拒绝错误签名与旧提交入口 | [双端联动测试](../../飞书工作入口/test_outer_channels.py) |
| 可信验收与结果转移 | 17/17 个 V2 验收测试方法通过。包括未知或不安全的验证规则、静态产物不能代替实际运行、人工约束缺口、当前版本交付、前序回归、证据篡改与过时回执 | [V2 验收测试](../../网页工作台/superagent/test_outer_verification.py) |
| 非 CSV 行为核验 | 用户态真实 Seatbelt 中，1/1 个正确实现通过目标验收，2/2 个独立合成输入得到预期输出。3/3 个错误变体未获得全部必需项通过，分别是恒定输出、语法错误和入口缺失。恒定输出碰巧匹配第一个输入，第二个输入将它拦住 | [真实 Python CLI 样本](../../网页工作台/superagent/test_outer_verification.py) |
| 隔离策略效果 | 2/2 个旧验证器宿主机测试通过。包含实际本地导入、输出不匹配、拒绝写入和硬超时。V2 的真实 CLI 样本与这两项合计为 3/3 个真实 Seatbelt 专用方法 | [旧验证器测试](../../网页工作台/superagent/test_goal_checks.py) |
| 协调与恢复 | 2/2 个跨阶段样本分别完成修复和有限重规划。重规划走实际模型适配器与启动检查，CLI 返回为固定替身。旧路由和 Run 传输分别有 5/5、4/4 个替身测试方法通过，覆盖批准冲突、局部重试、ACK 丢失及终态对账 | [V2 验收测试](../../网页工作台/superagent/test_outer_verification.py)、[旧路由夹具](../../网页工作台/superagent/test_router.py)、[Run 传输夹具](../../网页工作台/superagent/test_goal_transport.py) |

## 复现

使用上述候选提交的独立克隆。在有权限启动本机进程和 Unix socket 且可应用独立 Seatbelt 策略的 macOS 会话运行。桌面代码沙箱可能拒绝在其内再次应用 Seatbelt。若隔离探测失败，不能按真实行为测试通过计数。

统一入口为 `GOAL_CHECKS_REAL_SEATBELT=1 python3 -B scripts/check.py`。本轮使用了当时尚未纳入候选提交的统一入口文件，其 SHA-256 为 `8e821ab3e90943c3b5a9dfc995f06776b77317b5935db0cb1a83a53ee689c7ba`。在候选提交上也可直接运行等价的逐组命令。

```bash
python3 -B scripts/check_repository.py
git diff --check origin/main...HEAD
cd 网页工作台
for file in *.js; do node --check "$file" || exit; done
node --test test_journey.mjs test_presentation.mjs
python3 -B -m unittest discover -q
GOAL_CHECKS_REAL_SEATBELT=1 PYTHONPATH="$PWD:$PWD/superagent" python3 -B -m unittest \
  superagent.test_goal_checks superagent.test_goal_codex superagent.test_goal_engine \
  superagent.test_goal_team_files superagent.test_goal_team_plan superagent.test_goal_transport \
  superagent.test_outer_intake superagent.test_outer_model superagent.test_outer_verification superagent.test_router
cd ../飞书工作入口
python3 -B -m unittest discover -q
```

## 证据边界

- 这组场景主要衡量确定性约束、契约和状态转换。替身按固定脚本返回，无法估计真实模型的路由准确率、错误自动归属率或澄清召回和误问率。没有独立标注集、生产样本或者随机采样。
- 两个真实 CLI 输入能识别本轮三个错误变体。对未见输入泛化、手工或主观质量判断、真正浏览器交互和部署后的 API 行为尚无结果。静态 HTML 与 JSON 经过真实本机读取，覆盖其已登记的有限检查。
- 双端测试证明本机签名、会话、幂等和数据库路径。飞书平台的实际事件重投、按钮回调、消息投递与移动端显示，仍需要真实渠道样本。
- 本次没有在线模型或生产 Hub 全链路数据。调用费用、真实推理质量、实际负载、跨阶段自主规划质量和 V2 执行预算耗尽场景未单独测量。恢复测试以可控故障注入为主，无法据此估计生产恢复成功率。

## 真实模型的补充样本

使用既有登录的 Codex 与同一接单入口，五条中文消息各调用 Intake Assessor 一次。每条消息的两张既有采购报告工单由固定替身预置，待评消息由真实模型判断。事前标注、输入和评分程序见[复跑脚本](../../scripts/evaluate_routes.py)，每次输出见[脱敏逐条汇总](../../evidence/route-samples.jsonl)。

| 指标 | 本次观察 |
| --- | --- |
| 路由符合事前预期 | 4/5。不明确交付格式的报告需求被转为先选择既有报告工单；原标注期望先建立草案并澄清格式 |
| 错误自动归属 | 3 条无明确绑定的输入中，0 条被自动附加到已有工单。两条进入显式选单 |
| 澄清 | 模型为 2/5 条消息提出问题，这两条实际进入工单选择。已绑定状态查询、已绑定范围变更和完整新需求均没有提出额外问题。标注为必须澄清交付格式的 1 条在首轮尚未进入格式澄清 |
| 调用与费用 | 五条输入合计 77,851 input tokens、2,283 output tokens，其中 26,368 个输入 tokens 命中缓存。通过现有登录执行，CLI 回执不含实际金额 |

会议排期的独立新任务还调用了真实接单、验收设计、规划和实施角色。验收设计覆盖三个明确给出的例子，另增加乱序及重复区间。四项分别运行两次均通过。暂停及恢复复验使用原批准版本。拒绝恒定输出等不合格变体的独立对照见上表。会议工单只需一次合成授权，首次终验发生一次环境回执兼容性阻塞，修复后由测试操作者暂停并续办。[工单证据](../../evidence/meeting/verification.json)
