# AFR v1 原生命令 Agent

适配器以独立进程调用本机 AFR v1 CLI。查询文件和资料库只在本机读取。原生 Autoloop 处理事件去重、排队、Run 领取和回执。

## 配置

试验生成的 candidate.rules.json 是规则模板。通过 afr-local 来源的 afr.lookup.requested 事件触发 afr-local-lookup Agent。实际部署时使用目标实例已授权的本机 Worker，并通过正常规则审核。命令固定脚本、解释器、AFR 仓库路径、知识库和请求目录，均为绝对路径。源事件不能改变这些配置。

事件只传请求编号。id 和 subject 可使用同一个随机编号，type 固定为 afr.lookup.requested。私有请求目录须预先存放以 subject 命名的 JSON 文件。文件内提供 query，或者提供 ref 与 revision。提交后保留原请求文件。不要把原始查询写进事件 detail 及 summary。

回执是单行 JSON，含 version、request_id、status、verified_count 和 references。references 只含 ref 和 revision。适配器回读段落后做 SHA-256 校验。正文、标题、路径、链接、查询、子进程原始错误均不会转发。ok、no_match、revision_changed 是业务状态。Run 的 done 状态只说明回执已登记。

## 复跑合成验收

从仓库根运行，需要 AFR v1 CLI 以及其合成 Markdown 夹具、Python 3.11+ 和已安装的 Autoloop。

    trial=$(mktemp -d /private/tmp/autoloop-afr-proof-XXXXXX)
    python3 -B scripts/live_afr_command.py "$trial" \
      --afr-root "$AFR_ROOT" --afr-python "$AFR_PYTHON" \
      --node "$AUTOLOOP_NODE" --autoloop-entry "$AUTOLOOP_ENTRY"

可选 --ticket wb-… 用于关联隔离事件与 Workbench 工单，并不会提交或批准工单。证据保存在试验目录的 evidence.json。结束时关闭本次新启动的 Hub 和 Worker，个人配置、私有请求和部署资料留在各自位置。

首次试验的固定回环端口已被占用，Hub 在启动阶段退出。复跑用系统分配的回环端口。成功、无命中、旧版本失败和改后重新检索均经过真实 Hub 和 Worker。为验证版本失效，只改动合成库副本，未调用模型。

要在 V2 作品中使用真实运行结果，可在其已授权、尚未实施或已停稳的工作区导入回执。使用下列命令提供只读的原生 Hub 数据库和目标工单号。导出器核对来源、事件类型、规则、Agent、工单与请求号，只保留已结束的 Run 编号、结果和引用摘要；重复导出相同内容可以重放。工单的新输入要在计划或恢复实施前就位。随后按正常验收契约运行。

    python3 -B scripts/export_afr_receipts.py --hub-db "$HUB_DB" \
      --ticket "$TICKET" --output "$APPROVED_WORKSPACE/afr-receipt.json"
