# AFR 工单研究 Agent 试点

一次已授权的 Autoloop command Run 对应一次研究。触发规则使用来源 `afr-local` 和事件类型 `afr.research.requested`。事件只传随机请求编号，`id` 和 `subject` 使用同一个编号。工作台先在私有收件目录原子写入 `<编号>.json`，然后派发事件。保持该文件不变直到 Run 结束。首轮、追问和发生失败后的再派发使用各自的事件编号。

```json
{"ticket":"wb-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","query":"请比较这几份材料对扩容的意见。","parent":null}
```

追问把 `parent` 设为同单最近一次成功的请求编号。Worker 会重新读回已保存的版本，再把上轮问题、观点、引用和开放问题传给已授权的模型。改版或找不到原段时返回业务错误。前一轮白板仍可打开。当前无法在一次追问中自动改用新的版本。历史记录带本机完整性校验，同一编号和请求重复运行时沿用原交付，内容冲突时拒绝。失败的 Run 不生成交付目录。模型执行阶段失败会在本工单的隐藏记录 `.failed-<编号>.json` 保存稳定错误码，诊断记录只在本机。Hub 收到的 JSON 只有 `version`、`request_id`、`status` 和 `verified_count`。Run 的 `done` 表示命令已回执，研究是否有结果要读取 `status`。

## 固定 Worker 配置

```sh
python3 -B scripts/afr_research_agent.py \
  --afr-root "$AFR_ROOT" --vault "$AFR_VAULT" \
  --requests "$PRIVATE_INBOX" --output "$DELIVERY_DIRECTORY"
```

在规则中将上面各路径设为绝对路径，命令 cwd 与知识库分开，Worker 的超时应大于研究时限。默认研究时限是 360 秒，可通过固定参数 `--timeout` 调整。研究器沿用 AFR Markdown 分段、检索、固定版本的规则。打开文件时逐级核对目录并拒绝符号链接及多重硬链接。程序按工单控制动作、读回数和版本，最多读 24 段，每个批次最多 6 段。白板首页和记录中的 `coverage` 来自程序计数，跟模型写出的观点分开。日期筛选需要可核对的入库记录，当前试点没有接入入库时间表。

默认模式是在本机匹配关键词和摘录引用。要在真实本机资料上启用观点综合，部署者可添加 `--local-model-command /绝对路径/适配器`。适配器用 stdin 接收一个包含 `prompt`、`schema`、`context` 的 JSON，用 stdout 返回一个符合 schema 的 JSON 动作。macOS Seatbelt 启动适配器并拒绝网络和写文件。需安装可在本机无网运行的推理后端。其他系统或 Seatbelt 无法启动时，该模式失败并保留错误回执。

## 合成模型联调

```sh
python3 -B scripts/afr_research_agent.py --init-synthetic "$TRIAL/synthetic-vault"
```

给隔离规则追加 `--synthetic-codex /绝对路径/codex`，并指向上面生成的合成库。脚本会逐条比对精确的合成正文和版本。自动复跑使用下面的问题。

- 首轮「梳理晨雾试点的七份材料，比较不同观点，注明出处和未决问题。」
- 追问「继续研究晨雾试点：哪些来源解释了分歧，还有哪些问题需要复核？」

体验入口如已逐轮显式征得本人同意，可给隔离规则另外添加 `--allow-synthetic-query-consent`，由本机请求文件提供 `"consent":"synthetic_query_to_codex"`。该许可覆盖本轮提交的问题、固定合成资料及本单此前已经授权的合成研究记录。模型外发时先核验合成范围、历史签名以及每个当前证据版本，再构造最小投影。Codex 临时会话里的 shell、浏览器、MCP、网页搜索及子 Agent 均由现有结构化调用入口关闭。正式知识库使用无外发模式。

## 打开交付

本机目录为 `<output>/<ticket>/<request_id>/`。在 Obsidian 打开 `研究白板.canvas`，点击出处卡可在同轮 `原文出处.md` 打开对应段落。将 `--output` 放在已经打开的 Obsidian 库内，或者指定包含输出目录的 `--delivery-vault`。若输出位于源库之外且没有明确指定交付库，使用输出根目录作为独立 Obsidian 库。引用记录、固定原文及研究历史留在受控目录，输出目录的隐藏 HMAC 密钥应保留在本机。

```sh
python3 -B -m unittest discover -s scripts -p 'test_afr_research.py'
```

## 从工作台体验一次真实研究

试点启动器复用原生 Hub 与 command Worker。每个实例固定一张工单和一个合成库，工作台中每次发送或追问都会产生本单的一次研究 Run。数据目录保留源事件、工单私有请求和版本归档。可用相同参数从原目录恢复。正式知识库的本地观察器与该实例各自运行。

```sh
stage=$(mktemp -d /private/tmp/afr-research.XXXXXX)
"$AFR_PYTHON" -B scripts/live_afr_research.py \
  --stage "$stage" --ticket "$TICKET" --afr-root "$AFR_ROOT" --afr-python "$AFR_PYTHON" \
  --node "$AUTOLOOP_NODE" --autoloop-entry "$AUTOLOOP_ENTRY" --codex "$CODEX_CLI"
```

收到 READY 后，在相应工单「补充资料」保存一行 `AFR 研究入口 http://127.0.0.1:8767/research/<完整工单号>`。已接入的桌面看板只识别与当前工单完全匹配的固定回环地址，显示「打开本单研究」。页面先展示本轮结果，可逐条展开出处，或切换到保存的白板。追问使用最近一次成功交付作为父轮。提交前逐轮确认本次问题、合成来源与已授权的本单历史将交给 Codex。无可用的模型时保留真实失败回执。

如电脑已有专用的 Obsidian 试点库，可在启动时增加 `--obsidian-preview-vault "$REGISTERED_TRIAL_VAULT"`。工作台中的「送到 Obsidian 试点库」导出所选版本的白板和固定出处。已交付版本可直接点「在 Obsidian 打开」。程序会保留用户后来编辑过的副本。个人模型外发权限由各自的部署配置决定，这个体验启动器只使用七份已校验的合成材料。
