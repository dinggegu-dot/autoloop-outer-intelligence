# 全 GPT 工单的本机隔离试用

从 [V2 正式灰度手册](formal-outer-canary.md) 核对现役入口。走团队路线时，网页源码使用正式形态，SQLite、Hub、Worker、Codex 设置、试验令牌与下载均放在新目录。启动器为每个服务分配不同于 8765 的本机端口，只复制允许的源码和素材。

## 启动与同单验证

在候选仓库根以本机 Python 3.11 运行下列入口。参数应指向实际安装位置及已获授权的独立 Codex 登录配置。若现役使用外层灰度，指定安装记录中的恢复点。启动器仅使用摘要核对通过的原入口三份源码，便于在隔离副本接上最新团队路由。

```sh
python3 -B scripts/run_goal_team_pilot.py \
  --formal-source "$formal_source_dir" \
  --formal-canary-recovery "$verified_pre_canary_dir" \
  --codex-home "$isolated_codex_config" \
  --workbench-only
```

- 启动器输出隔离地址和实例目录。打开地址的工单页，从「新建工单」勾选目标模式。用合成需求登记，看原单目标、合同、权限及预算后批准。在原工单等独立验证后下载。查看偏好问题、退回和签收请使用各自具名操作。
- 验收初稿预览时，用两项可分别核验的合成文件建单，团队审稿选「从第 2 步审稿并请我定方向」。批准一次，等第 2 步回到「等你拍板」。在原单展开「查看这版初稿」，切换文件、查看完整源码，并从顾问引用跳回对应文件。核对选项旁说明的修改和保留内容。仅检查预览时，让原单停在待决定状态。
- 本步返修试验在上述命令追加 `--browser-fixture --inject-inner-repair`。从普通新单入口提交合成页面 `inner-repair-demo.html`，核对合同分别检查标题 `Daily Brief`、一级标题 `Daily Tasks` 和元素 `task-count`，以及标题 `Daily Brief`、一级标题 `Status Notes` 和元素 `task-notes`。两项静态检查绑定同一实施阶段。夹具在首次模型制作完成后仅把 `Daily Tasks` 改成 `Draft Tasks`。从原单的「返修与复验」核对问题、保留项、改动、负责人检查和前后版本。通过后再核对外层独立验收与下载。此模式仅启动隔离工作台、Hub、Worker 和 Codex。
- 阶段一的四类故障验收以 `--phase-one-fixture` 分别选择 `verification`、`retained-file`、`budget` 或 `recovery`。每个场景用独立实例，`scripts/live_goal_team_phase_one.py case <场景>` 列出可填入普通新单的合成内容。预算场景在批准前显示四个角色轮次，等待后续追加，再核对续办仍从原候选开始。恢复场景在封存凭证落盘后中断隔离 Worker。中台可能按原生退避等待同一轮重领。重领核对封存回执和版本，由外层独立验收。
- `--phase-one-fixture review-recovery` 在返修执行者写完新版、待复验凭证落盘后中断隔离 Worker。使用 `scripts/live_goal_team_phase_one.py inspect <实例目录> <原工单号>` 核对恢复后只有一轮只读负责人和一轮外层独立验收，且最终下载与中断时的新版一致。
- `python3 -B scripts/live_goal_team_short.py "$instance_dir" --case short` 运行最小文件试验，`--case preference` 准备三组样例后停止自动操作。脚本标记审批为试验者操作。真实偏好和签收由本人从原单操作。脚本属于接口实测，界面操作单独记录。
- 启动器的 `--fixture control`、`--fixture regression`、`--fixture pause-stale` 和 `--fixture nonterminal-regression` 各使用独立试验目录。故障夹具会在确定的阶段二覆盖候选中的合成搜索脚本。回归夹具在收到角色结果、业务回执尚未提交时重启 Worker。非末阶段夹具要求在第三阶段派工前发现回归和完成返工，最后核对三份下载。查看试验目录中的 `acceptance.json`。
- `--fixture preference-race` 使用新建合成单和试验操作员身份。它在首轮真实样例回执提交前暂停，恢复后用新版问题让网页及配对 DSH 接口同时答复。核对后端只采用一条、拒绝错投与旧版、绑定原授权复验并读回已验下载。此次接口试验与本人样例单分开。
- `--case blind` 准备同题盲评。选 `--test-ballot` 时只允许独立合成试单，脚本提交带试验归属的偏好和未评选票，验证来源封存、下载闭锁、重复锁票和结果回读。评价贴切程度与偏好采纳需要本人逐例操作。[隔离用合成输入](../../网页工作台/验收样例/匿名评测/留出输入.json)
- 真人评分的独立对照用 `--case blind --prepare-ballot`。试验操作员准备合成方向和匿名材料，脚本在真实机器验收通过且双路来源仍封存时停住。此时由本人在原单逐例评阅并锁票。[隔离用合成输入](../../网页工作台/验收样例/匿名评测/留出输入.json)
- 本机仓库基线检查用 `python3 -B scripts/check.py`。回环、进程和宿主沙箱检查需要正常本机执行权限。保留日志类别，不归档源码副本外的正式材料及账号原件。

## 回退和诊断

1. 暂停接新试单，确认在途 Run 与角色进程结束。保留目标和验收版本、本人意见、实际产物、轮次、独立验收及下载索引。回执缺失且写入结果不可归属时先保持待核对。
2. 按 [V2 回退手册](goal-coordinator-rollback.md) 制作私有一致恢复点。兼容读取提交 `9edf822` 与后续新增记录能独立部署，退回新执行路线时先在副本验证老单和新单的回读顺序。验证已归档的下载、等待问题、已生效答复，再决定后续新单打开哪个入口。
3. 对已停的试点使用 `python3 -B scripts/recover_goal_team_pilot.py --source "$stopped_pilot_dir"` 重放只读恢复。它保留两个数据库及所需 WAL，在独立临时副本查询原工单和候选。然后核对文件摘要与监听进程，依步骤二单独复验活动工单续办。源码回退走 revert，私有配置、令牌、未脱敏日志和运行备份留本机。

专用恢复试验可运行 `python3 -B scripts/recover_goal_team_resume.py --formal-source "$isolated_source" --codex-home "$isolated_codex_config" --proxy "$approved_loopback_port" --evidence "$new_evidence_file"`。它另建私有合成工单，停机备份并恢复到该试点原路径。恢复后实际启动 Hub、Worker、Codex 和机器验收。[真实续办的脱敏记录](../../evidence/goal-team/active-resume-recovery.json)

实际首条合成短链路、角色权限探针与复核记录见 [V2 执行与验收记录](../progress/v2-status.md#全-gpt-工单与配套工作台)。
