# Phase E 五单容量回归

2026-11-19 在候选提交 `76059b77e771fe76faeb867559e62be3ee839c39` 的独立源码快照执行。每种容量各用一个全新的临时运行实例，使用本机已安装的 Autoloop Hub、Worker、SQLite 和 macOS Seatbelt。运行环境为 macOS 26.6.2 arm64、系统 Python 3.9.6。Codex 命令由仓库内的离线协议夹具提供，运行前核对字节一致，启动 PATH 不包含真实 Codex。没有模型调用、渠道消息或生产操作。

| 配置容量 | Hub 同时领取峰值 | 工单完成 | Run 正常终态 | Run 重试 | 终验必需项通过 | 可读取交付文件 | 前五个工作包 Run 对应的独立工单 | 首次领取至最终 Run 结束 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 4 | 4 | 5/5 | 25/25 | 0 | 10/10 | 10/10 | 5/5 | 19.86 秒 |
| 1 | 1 | 5/5 | 25/25 | 0 | 10/10 | 10/10 | 5/5 | 60.76 秒 | |

两个配置均按每单一次批准、一次规划、两个独立工作包、一次合流和一次复核进行。并发峰值来自真实 Hub Run 的领取和完成时间区间。前五个工作包按 Hub Run ID 排序，用于检测队列分配是否先集中在少数工单。耗时为该次样本首个 Run 领取到最后一个 Run 结束的窗长。

这是一组既有目标路线回归，输入为合成 CSV，工作包和审核回复使用离线协议夹具，验证器实际执行交付程序。每个容量只执行一轮。观测可用于核对容量上限与固定五单样本的分配结果，不能估计线上吞吐、成本或 V2 模型自主规划表现。

## 复跑

从已检出的候选提交根目录，在允许绑定本机回环端口并应用本机 Seatbelt 策略的宿主会话运行。使用已安装的 Autoloop 引擎和两个空闲回环端口。

```bash
set -e
umask 077
capacity_root=$(mktemp -d /private/tmp/autoloop-capacity-XXXXXX)
mkdir -p "$capacity_root/fixture-bin" "$capacity_root/capacity-4/runtime" "$capacity_root/capacity-1/runtime"
sample='网页工作台/验收样例/多工单容量'
install -m 700 "$sample/fixture.py" "$capacity_root/fixture-bin/codex"
cmp "$sample/fixture.py" "$capacity_root/fixture-bin/codex"

run_capacity() {
  env -u CODEX_HOME PATH="$capacity_root/fixture-bin:/usr/bin:/bin:/usr/sbin:/sbin" \
    /bin/bash '网页工作台/superagent/isolated_goal_hub.sh' \
      --team-capacity "$1" "$capacity_root/capacity-$1/runtime" "$2" -- \
      /usr/bin/python3 -B "$sample/五单验收.py"
}

run_capacity 4 43641
run_capacity 1 43642
```

正式入口核对夹具、任务数、Run 终态、实际峰值、单次批准、工作包顺序、逐项验收和可读交付文件。原始输出中的运行数据、数据库及临时密钥留在本机，仓库仅保留本页脱敏摘要。
