# 开发工单交付回执

开发工单完成独立验收或本人签收后，可以把这版归档交给已有 GitHub 交付流程。工单详情展示该流程真实上报的 PR、审查、检查、合并、Ship 和结果。工单的产物验收与线上验证分别记账。

## 本机登记授权

在受信任的本机终端操作。运行账本应使用工作台实际启动时传入的 `--runtime` 目录，程序只接受实际存在且由本人持有的私有账本。

```sh
python3 -B scripts/record_ticket_delivery.py --runtime "$WORKBENCH_RUNTIME" register "$TICKET"
python3 -B scripts/record_ticket_delivery.py --runtime "$WORKBENCH_RUNTIME" authorize "$TICKET" \
  --mandate "$MANDATE_REVISION" --digest "$VERIFIED_ARTIFACT_SHA256" \
  --repo "$REPO" --number "$PR_NUMBER" --head "$PR_HEAD_SHA" --mode release
```

`register` 只登记开发交付待办。另签时核对原单的授权版本、已验归档摘要和 PR 的完整提交 SHA。独立的 `authorize` 只记录人工绑定的授权来源和后续报告，它不验证 PR 文件是否与工单归档一致。需要只审查时用 `--mode review`，并同时按现有 GitHub 交付规则选择只审查。PR 提交变化需要重新核对和授权，已有绑定不会跟随 PR 跳到新提交。工作台记录只限这张工单和这个提交。GitHub 操作者仍按现有可信交付配置执行审批、标签、检查、审查、合并和 Ship。工单另签会写入本机授权账本。

## 开 PR 并交既有插件

已安装 GitHub CLI 且本人已有对应仓库权限时，可以从已验工单一步进入交付。先准备、推送已经核对的同仓分支，并取得完整提交 SHA。在本机明确选择只审查或允许发布，再运行以下命令。

```sh
python3 -B scripts/record_ticket_delivery.py --runtime "$WORKBENCH_RUNTIME" handoff "$TICKET" \
  --mandate "$MANDATE_REVISION" --digest "$VERIFIED_ARTIFACT_SHA256" \
  --repo "$REPO" --base main --branch "$PUSHED_BRANCH" --head "$PR_HEAD_SHA" \
  --mode release --node "$TRUSTED_NODE_24"
```

程序先读取目标仓库 main 的 `ops/delivery.json`，请安装的交付插件验证配置，再核对已推送分支。提交树里每个对应路径必须是普通 Git 文件，字节的 SHA-256 要逐个与已验归档一致。本入口使用同路径映射，遇到路径或字节不符会在建 PR 前停下。通过后，工单授权才会显示核验的文件数。程序还核对整个 PR 的变更清单，额外文件单独列出。额外的脚本、可执行文件、工作流、交付设置或无法证明是普通资料的文件需要逐项增加 `--allow-extra-file 路径`，这只授权发布这条额外改动。原单仍标明它没有经过本次工单验收。可复用同一分支、base 和 SHA 的开放 PR，未找到时创建草稿，再核对 PR 和分支指向。提交若在创建请求与 GitHub 返回之间变化，草稿保留给本人核对，交付不会启动。发布过程使用插件自身的审查、检查、合并和健康检查，由绑定的另签及精确 SHA 约束；只审查模式没有合并权限。若 main 前移需要同步分支，请先推送新提交，再重新核对和授权。已合并的原 PR 可用同一授权重试 Ship 和健康核验，即使源分支已经删除。协作过程中会重新核对授权和远端配置。配置包含自定义本机审查命令或管理员合并时，程序停止，请按已有人工受信交付流程办理。仓库按自己的策略免审时，本工单入口也会在合并前停下并记录缺少独立审查证据。需要再次提交时，先由现有可信规则完成独立审查。同一运行账本中，同一仓库、目标分支和源分支同时只允许一个手动交付，锁随实际进程退出释放。这条执行路线不会自动发 GitHub 交付标签或与声称已接单的其他交付轮并行接管同一 PR。

未能读取或验证仓库交付配置时，操作在创建 PR 前停止。仓库及目标权限须由本人显式选定。命令创建 PR 和处理远端交付的能力仅用于获相应授权的仓库与提交。

## 让已有插件上报

对已授权的仓库，在现有 delivery Worker 环境设置 `AUTOLOOP_TICKET_DELIVERY_RUNTIME` 为该账本目录。给其固定命令增加 `--progress-command /绝对路径/受信版本/scripts/record_ticket_delivery.py`，并确保固定命令用的脚本有执行权限。可信交付程序从标准输入发送回执。无需启用新的网络端点，也无需传递 GitHub 凭据到工作台。在试跑或条件测试中使用独立账本。

回单核对命令如下。

```sh
python3 -B scripts/record_ticket_delivery.py --runtime "$WORKBENCH_RUNTIME" status "$TICKET"
```

- 未登记的普通工单没有发布区域。已登记但还未另签的开发工单显示未授权。已另签但暂未收到插件报告时显示等待回执。
- 收到 `merged` 时只记合并提交。收到 `docs_only` 时记录文档调整及 Ship 证据。只有现有插件在完成线上核验后发来 `live`、合并提交和 Ship 编号，同一授权版本才显示已核验上线。
- 审查退回、暂时失败重试、超出只审查范围及合并结果使用各自状态。错误仓库、PR、提交、过时授权或旧归档的回执不会改动当前单。按工单派发的回执以另签编号绑定。在同一个 PR 和 head 多次另签后，只有带准确另签编号的回执可更新当前单。原工具进度回调本身可能失败，等待回执时回到原工具核对其 GitHub 结果。

验证使用临时账本和合成仓库。已使用本机安装版 delivery 插件的 `runHook` 执行回调，核实收到合并 SHA 和 Ship 编号。该验证无需访问 GitHub，未执行真实发布。
