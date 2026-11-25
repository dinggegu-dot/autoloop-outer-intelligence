# 目标漫游局 · Autoloop V2

Autoloop 让你把一个目标交给工作台，在同一张工单上批准、看稿、提意见和收成果。外层负责记录授权、安排执行和独立验收。内层用 Codex 会话完成眼前这一步，按需要找顾问提案、请执行者修改，再由负责人复验。其他有授权且有执行名额的工单可以继续推进。

## 一张工单，两层分工

[![Autoloop 工作台与 Codex 两层协作流程](docs/assets/workbench-two-layer.svg)](docs/assets/workbench-two-layer.svg)

图中每个编号都有对应的实现说明。可以查到流转条件、源代码和原单里留下的证据。[逐环节实现说明](docs/architecture/workbench-two-layer.md)

## 正式工作台怎么用

| 想做什么 | 到哪里操作 | 能核对什么 |
| --- | --- | --- |
| 提交目标 | 「新建工单」选择「使用 Codex 团队」，确认本单允许发送的文字后登记 | 原话、可交付成果、验收条件和批准预算 |
| 处理要你决定的事 | 首页「待我决定」 | 普通工单和 Codex 工单的等待原因、下一步，以及原单对应位置 |
| 看方案、初稿和返修 | 「进度」看审稿与返修。待定方向时，从原单首区点「查看这版初稿」 | 双方方案、顾问发现的问题、给执行者的清单、前后版本和复验 |
| 看系统为什么重新派工 | 原单「决定与记录」展开「角色、工具与派工记录」 | 目标协调读过哪些本单证据、要修哪一步、保留什么、接下来复验什么 |
| 签收或下载 | 原单「成果与验收」 | 机器检查、需要本人核对的条款、签收或退回的版本、已验归档 |

## 当前版本

2026-11-25，正式本机工作台 **8765** 已上线统一待办、角色协作证据和交付预览。Chrome 从首页进入已有的合成索引原单，核对了 **8/8** 机器验收和两个归档文件。真实多角色简报、两版退回签收及 Worker 重启后的协作恢复有各自的隔离验收记录。[本次发布](https://github.com/dinggegu-dot/autoloop-outer-intelligence/pull/15#issuecomment-7160223953) · [已部署的源码 903fb0c](https://github.com/dinggegu-dot/autoloop-outer-intelligence/tree/903fb0c19badc8479127be882b4ff8603b53afd4) · [隔离简报记录](https://github.com/dinggegu-dot/autoloop-outer-intelligence/blob/903fb0c19badc8479127be882b4ff8603b53afd4/evidence/goal-team/authorized-workbench-live.json)

这张图展示的是正式的**工作台与 Codex** 路线。飞书工单接续、DSH 隔离实验和 AFR 研究交接都有各自的连接和验收范围。[运行方式与边界](docs/architecture/workbench-two-layer.md#d3-运行方式和其他入口) · [AFR 研究交接记录](https://github.com/dinggegu-dot/autoloop-outer-intelligence/blob/903fb0c19badc8479127be882b4ff8603b53afd4/docs/testing/afr-integration.md) · [配套知识库 AFR](https://github.com/dinggegu-dot/atom-flow-research)

## 本机浏览源码

可运行的 V2 代码在 `codex/v2-outer-intelligence`。具备仓库访问权和本机 Python 3 时，可用独立临时目录打开网页预览。

~~~sh
git clone --branch codex/v2-outer-intelligence https://github.com/dinggegu-dot/autoloop-outer-intelligence.git
cd autoloop-outer-intelligence
preview=$(mktemp -d)
python3 -B 网页工作台/server.py --port 8875 --runtime "$preview/workbench" --hub-home "$preview/hub"
~~~

打开 <http://127.0.0.1:8875/> 浏览候选界面。正式团队入口、模型执行、Worker 和飞书桥根据各自手册配置。[试点启动](https://github.com/dinggegu-dot/autoloop-outer-intelligence/blob/903fb0c19badc8479127be882b4ff8603b53afd4/docs/operations/pilot.md) · [正式升级与回退](https://github.com/dinggegu-dot/autoloop-outer-intelligence/blob/903fb0c19badc8479127be882b4ff8603b53afd4/docs/operations/formal-outer-canary.md) · [回归入口](https://github.com/dinggegu-dot/autoloop-outer-intelligence/blob/903fb0c19badc8479127be882b4ff8603b53afd4/scripts/check.py) · [长期架构与验收边界](https://github.com/dinggegu-dot/autoloop-outer-intelligence/blob/903fb0c19badc8479127be882b4ff8603b53afd4/docs/architecture/architecture-alignment.md)
