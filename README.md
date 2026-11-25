# 目标漫游局 · Autoloop 工作台

在首页写下目标，Router 会给每张工单安排自己的团队。两张工单同时办，给其中一张补充资料，消息就沿订阅回到它自己的团队。

[![两层分工与工单订阅](docs/assets/router-ticket-teams.svg)](docs/assets/router-ticket-teams.svg)

## 怎么用

1. 从「新建工单」填写目标和完成标准，确认允许发送这次填写的文字。
2. 回原单核对目标、验收标准和预算。简单步骤由 GPT‑6.2 Sol 直接实施，复杂步骤由 Sol 负责人和只读 GPT‑6.2 Astra 顾问各提一案，比较后再做。
3. 从原单补充资料、查看实际草稿，或在需要时拍板。最终从同一张工单签收、下载已验版本。

[这版的操作、角色与验收](https://github.com/dinggegu-dot/autoloop-outer-intelligence/blob/8ef5fa5bddd2ab31fba8459317d804eb93c1c49b/docs/product/router-ticket-teams.md) · [源码变更与检查](https://github.com/dinggegu-dot/autoloop-outer-intelligence/pull/19) · [正式升级与回退](https://github.com/dinggegu-dot/autoloop-outer-intelligence/blob/8ef5fa5bddd2ab31fba8459317d804eb93c1c49b/docs/operations/formal-outer-canary.md)

## 其他入口

飞书工单接续、DSH 隔离实验和 AFR 研究交接都有各自的连接和验收范围。[运行方式与边界](https://github.com/dinggegu-dot/autoloop-outer-intelligence/blob/8ef5fa5bddd2ab31fba8459317d804eb93c1c49b/docs/architecture/architecture-alignment.md) · [AFR 研究交接记录](https://github.com/dinggegu-dot/autoloop-outer-intelligence/blob/903fb0c19badc8479127be882b4ff8603b53afd4/docs/testing/afr-integration.md) · [配套知识库 AFR](https://github.com/dinggegu-dot/atom-flow-research)

## 本机浏览源码

可运行的 V2 代码在 `codex/v2-outer-intelligence`。具备仓库访问权和本机 Python 3 时，可用独立临时目录打开网页预览。

~~~sh
git clone --branch codex/v2-outer-intelligence https://github.com/dinggegu-dot/autoloop-outer-intelligence.git
cd autoloop-outer-intelligence
preview=$(mktemp -d)
python3 -B 网页工作台/server.py --port 8875 --runtime "$preview/workbench" --hub-home "$preview/hub"
~~~

打开 <http://127.0.0.1:8875/> 浏览候选界面。正式团队入口、模型执行、Worker 和飞书桥根据各自手册配置。[试点启动](https://github.com/dinggegu-dot/autoloop-outer-intelligence/blob/8ef5fa5bddd2ab31fba8459317d804eb93c1c49b/docs/operations/pilot.md) · [正式升级与回退](https://github.com/dinggegu-dot/autoloop-outer-intelligence/blob/8ef5fa5bddd2ab31fba8459317d804eb93c1c49b/docs/operations/formal-outer-canary.md) · [回归入口](https://github.com/dinggegu-dot/autoloop-outer-intelligence/blob/8ef5fa5bddd2ab31fba8459317d804eb93c1c49b/scripts/check.py) · [长期架构与验收边界](https://github.com/dinggegu-dot/autoloop-outer-intelligence/blob/8ef5fa5bddd2ab31fba8459317d804eb93c1c49b/docs/architecture/architecture-alignment.md)
