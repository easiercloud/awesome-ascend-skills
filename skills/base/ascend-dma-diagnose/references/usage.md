# 运行和复现

安装命令从你选择的工作目录执行；后面的测试命令从已安装或源码中的 skill 目录执行。例子中的路径、设备和容器名须替换为本机实际值，不包含真实机器地址或认证信息。

## 安装入口

### 当前 GitHub 功能分支 → 本地 Codex

下面是首次安装到默认 `~/.codex/skills` 的简便方式；设置了 `CODEX_HOME` 时使用 `$CODEX_HOME/skills`。这里只提供命令，不会自动安装软件或修改本机 skill。目标已有同名目录时先比对并保留自己的改动。

```bash
git clone --single-branch --branch codex/ascend-dma-diagnose \
  https://github.com/easiercloud/awesome-ascend-skills.git awesome-ascend-skills-dma
cd awesome-ascend-skills-dma
codex_skills_dir="${CODEX_HOME:-$HOME/.codex}/skills"
mkdir -p "$codex_skills_dir"
cp -R skills/base/ascend-dma-diagnose "$codex_skills_dir/"
```

也可以从已经检出的仓库根目录，只运行上面最后三行。随后从安装目录查看 CLI；在 Codex 请求中可显式引用 `$ascend-dma-diagnose` 或提供 `SKILL.md` 的路径：

```bash
cd "${CODEX_HOME:-$HOME/.codex}/skills/ascend-dma-diagnose"
python3 scripts/dma.py --help
```

如果希望只供一个项目使用，从仓库根目录复制到该项目：

```bash
mkdir -p /path/to/project/.agents/skills
cp -R skills/base/ascend-dma-diagnose /path/to/project/.agents/skills/
```

### 主上游合并后的标准安装

功能尚未合并时，上游 main 不包含此 skill，使用前面的功能分支方式。合并后沿用本仓标准入口：

```bash
npx skills add ascend-ai-coding/awesome-ascend-skills -s ascend-dma-diagnose
# 或安装整个基础技能包
npx skills add https://github.com/ascend-ai-coding/awesome-ascend-skills/tree/main/skills/base -s '*'
```

这里用 Git 的 `--branch` 选择功能分支，不额外编造 npx 分支参数。CLI 使用 Python 3.9+ 标准库；硬件采集面向有 sysfs/proc 的 Linux 宿主机，torch 测试需要现有 torch/torch_npu/CANN，ACL 需要现有 CANN Runtime。平台、驱动或 API 不满足时保留 unsupported/failed，不承诺所有 CPU/NPU/Runtime 组合可测。非硬件环境可使用离线 replay/report；PMU 适配范围与 DMA backend 的可用性分开判断。

## 一条命令完成基准和诊断

从源码或项目内安装时，将下方 `cd` 替换成实际 skill 目录。后续代码块均以该目录为基准，各 run 使用自己的输出目录。

```bash
cd "${CODEX_HOME:-$HOME/.codex}/skills/ascend-dma-diagnose"
python3 scripts/dma.py run --out runs/dma-full --cards all \
  --memory pinned pageable --directions H2D D2H HD \
  --modes solo concurrent --backend torch --diagnose --performance require
```

这覆盖每个选择设备的 solo，以及选择集的并发三方向、两种 host 内存类型。`all` 按本机发现和验证的映射选择，支持不同设备数量；不是固定八卡。一次请求即可让 agent 继续设计异常对照，CLI 每轮默认追加上限由 `--max-diagnostic-cases` 控制。

需要激活已有环境或使用现有容器时：

```bash
python3 scripts/dma.py run --out runs/dma-container --cards all \
  --memory pinned pageable --directions H2D D2H HD \
  --modes solo concurrent --backend torch --diagnose --performance require \
  --env-script /path/to/set_env.sh --container existing-name
```

拓扑、CPU 策略和 PMU 在 host 采集；benchmark 在指定环境中执行。保留容器实际设备可见性、cpuset、版本和 mapping 证据，不把容器限制当成整机硬件。脚本不重启容器、不终止训练。连接与跳板使用你已有的项目工具或 SSH 配置；不要把口令写进脚本/hosts 文件。

只检查环境或预览矩阵、暂不启动 DMA。`plan` 使用已经采集的 topology；没有时先执行后文的 `collect`：

```bash
python3 scripts/dma.py doctor --out capabilities.json
# 可选：子进程实际检查现有库能否加载；不选择设备或分配 NPU 内存
python3 scripts/dma.py doctor --container existing-name --load-backend torch \
  --env-script /path/to/set_env.sh --out backend-capabilities.json
python3 scripts/dma.py plan --topology runs/topology-only/topology.json \
  --out planned-cases.json --cards all --memory pinned pageable \
  --directions H2D D2H HD --modes solo concurrent
```

## 单卡、子集和匹配 ACL 对照

只测逻辑设备 0 的单卡基准：

```bash
python3 scripts/dma.py run --out runs/single-0 --cards 0 \
  --memory pinned pageable --directions H2D D2H HD --modes solo \
  --backend torch --performance require
```

同时测一个经过验证的设备子集，并允许诊断：

```bash
python3 scripts/dma.py run --out runs/subset --cards 0 1 2 3 \
  --memory pinned pageable --directions H2D D2H HD \
  --modes solo concurrent --backend torch --diagnose --performance require
```

将同一矩阵换成底层 ACL 作匹配佐证，使用新输出目录：

```bash
python3 scripts/dma.py run --out runs/acl-control --cards 0 1 \
  --memory pinned pageable --directions H2D D2H HD \
  --modes solo concurrent --backend acl --performance require
```

比较前对齐 buffer、迭代、repeat、CPU/内存策略及 copy/sync 协议。默认 byte buffer 不做 dtype/布局转换。Pinned HD 使用两个设备 stream；pageable 同步 API 的 HD 使用两个 host 线程。具体协议在每个结果的 worker metadata 中，不因写了 HD 就假定硬件已完全重叠。

## 映射不确定时

多芯片、非连续物理 ID、可见设备重排或输出无法唯一关联时，提供显式 JSON 列表。下面是格式示例，BDF 必须替换为本机实际值：

```json
[
  {"logical_id": 0, "physical_id": 0, "chip_id": 0, "bdf": "0000:03:00.0"},
  {"logical_id": 1, "physical_id": 0, "chip_id": 1, "bdf": "0000:03:00.0"}
]
```

使用 `--device-map /path/to/device-map.json` 传给 `run`/`collect`。上面只演示格式；实际应对齐 backend 的完整可见逻辑设备集合，再用 `--cards` 选测试子集。连续 `0..N-1` 映射会与 Runtime device_count 核对，不能只列两项却仍让 backend 看见十六设备。容器的可见集可能不同于 host 全部物理设备，须明确匹配。相同 BDF 的两个 logical ID 只有在该多芯片映射已确证并显式给出时才接受，不按列表位置猜 chip。跨机器必须重新生成此文件。

## 只采集、生成报告和离线示例

```bash
python3 scripts/dma.py collect --out runs/topology-only
python3 scripts/dma.py report --run runs/dma-full
python3 scripts/dma.py replay --input examples/pcie-gen5-pairs.json --out runs/replay-example
```

`collect` 保存本机完整 PCI 树、命令与原始证据；`report` 重校验并渲染已有结果；`replay` 不访问 NPU。仓内 `pcie-gen5-pairs.json` 是真实历史测量的脱敏重放数据，保留原始数值观测和旧版记录格式，不是合成带宽。导入器独立核对样本、字节、时间、内容验证标记及原有汇总，并标为 `measurement_kind=historical_replay`。这可以分析历史结果，但不能称为在当前机器新测；脱敏公开样例未发布逐字原始系统 stdout 或原始私有 receipts，其证据边界仍须保留。

## 精确重跑一个 case

保留本机已采集且当前仍有效的 topology，将 case JSON 中的设备、方向和参数核对后运行：

```bash
python3 scripts/dma.py run-case --topology runs/dma-full/topology.json \
  --case runs/dma-full/cases/solo-0-pinned-H2D-torch.json --out runs/retest-single-0
```

Case 只使用已验证 logical ID。新目录保留旧结果；不要拿一台机器的归档 topology 在另一台直接执行。实际 case 文件名以 `cases/` 内保存的 ID 为准。

`reproduce.sh` 与 `reproduce-kit/` 保留可执行实现，不能替代原来的驱动、CANN 或框架安装。硬件 run 从 `execution-config.json` 带入容器、环境脚本、Python、CPU 策略和超时等执行选项，`reproduce-cases/` 去除已过期的 fleet gate/epoch，再由 `run-case` 执行；仍要核对当前环境与设备身份。历史 replay 生成的复现脚本只再次离线 replay，不启动历史 DMA。输出使用新目录，脚本内的相对路径以归档 run 目录为基准。

## 高性能、短烟测和续跑

| 选项 | 含义 |
| --- | --- |
| `--performance record` | CLI 默认；只保存 governor/频率证据，不能据此声称已经高性能 |
| `--performance require` | 核验已有 governor；不满足就返回明确状态 |
| `--performance set` | 本轮将可写 governor 设为 performance，退出时恢复；不改 BIOS |
| `--quick` | 64 MiB × 8 次、1 warmup、3 repeats 的短烟测；不能当峰值保证 |
| `--resume` | 校验原有完整结果后续跑，不把未验证/参数不匹配的文件当成功 |
| `--max-diagnostic-cases 12` | 一轮自动异常对照预算，可在既有授权范围内调整 |

用户明确要求高性能时，用 `set` 或以 `require` 证明已满足。若只记录现状，报告保留相应限制。Governor、CPU MHz 快照、BIOS、DDR/NPS 是不同证据，不互相替代。训练忙时等待或选择授权空闲机器，不能以删除进程解决。

`run`/`plan` 的基准参数可用 `--mib 256 --iters 32 --warmup 3 --repeats 5` 调整；这是默认值。`run-case` 从 case JSON 读取这些参数，不能在它后面添加 `--mib` 等矩阵参数。

| 选项 | 适用及边界 |
| --- | --- |
| `--cpu-node 0` | `run`/`plan` 的 CPU affinity 目标为 NUMA node ID，不是 socket ID；单 case 写在 JSON 中 |
| `--memory-node 0` | 申请内存绑定并严格验证实际页位置；EFAULT/权限受限不会被标成绑定成功 |
| `--event-timing` | 额外请求设备 stream Events，默认关闭；ACL pageable 明确 unsupported，不影响未请求 Event 的 host wall 测试 |
| `--python /path/to/python3` | 执行器 Python 路径；指定容器时是容器内路径，默认 `python3` |
| `--no-pmu` | `run`/`run-case` 不启动 host PMU observer；已有拓扑证据仍保留 |
| `--case-timeout 600` | 每个 case 的有界执行时间，单位秒 |
| `--wait-idle-timeout 300 --idle-stable 15` | 空闲准入最长等待与稳定窗口，单位秒；超时保留明确状态，不终止训练 |

CPU/内存和 Event 选项通过 `run`/`plan` 写入 case；`run-case` 读取 JSON。执行环境、CPU governor、准入和 PMU 选项则由 `run`/`run-case` 的执行参数指定。完整子命令参数以 `python3 scripts/dma.py <subcommand> --help` 为准。

## 用户代码与多机

用 `--source-code /path/to/benchmark` 指定需要检查的代码；可补充与安装版本一致的 torch_npu、op-plugin 源码。自动候选扫描是有范围上限的索引，深入方法见 [code-paths.md](code-paths.md)。不会改应用代码或凭关键词命中宣称因果。

```bash
python3 scripts/dma.py inspect-code --source /path/to/benchmark /path/to/version-matched-source \
  --out code-candidates.json
```

```bash
python3 scripts/dma.py fleet --hosts /path/to/hosts.json --out runs/fleet
```

多机 hosts 配置、同步放行、逐机结果和跨机比较按 [fleet.md](fleet.md)。不直接相加各主机未经同步的 monotonic 时间窗。

## 手动核对拓扑与 CPU I/O

CLI 已保存完整原始证据。需要在终端逐项确认时，可以查询：

```bash
npu-smi info
npu-smi info -m
npu-smi info -t topo
lspci -Dtv
lscpu
numactl --hardware
```

NPU 间 UB/SYS 等信息不能代替 Host↔NPU DMA 的 PCIe/CPU 路径。沿一张设备的真实 sysfs 祖先逐端口查看时，不预设固定三级 switch：

```bash
bdf=0000:03:00.0
pci_path=$(readlink -f "/sys/bus/pci/devices/$bdf")
while [[ -n "$pci_path" && "$pci_path" != / ]]; do
  node=${pci_path##*/}
  if [[ "$node" =~ ^[[:xdigit:]]{4}:[[:xdigit:]]{2}:[[:xdigit:]]{2}\.[0-7]$ ]]; then
    lspci -D -s "$node" -vv
  fi
  pci_path=${pci_path%/*}
done
```

对照端点、downstream、upstream 与 Root Port 的 Express 类型、LnkCap/LnkSta、AER，以及其他同类端口。BDF 的 bus 字节标识设备所在总线，不编码它上面全部桥的身份。Link 当前低于一端最大能力只是观察，仍需两端能力、布线与横向对照才能判断是否降 lane。

只发现 CPU PMU 能力，不配置任何计数器：

```bash
PYTHONPATH=scripts python3 - <<'PY'
import json
from pathlib import Path
from dmalib.pmu import discover
topology = json.loads(Path('runs/topology-only/topology.json').read_text())
print(json.dumps(discover(topology), ensure_ascii=False, indent=2))
PY
```

输出含支持状态、PMU format、符号事件、单位和型号限制；`io_mapping` 则保存在 `topology.json`。Intel/AMD 适配与流量校准边界见 [pmu-platforms.md](pmu-platforms.md)。不要用未知型号的 raw perf 编码“试一试”，或拿 IOMMU group 代替 IOM/IIO。

## 交付文件和解释

| 文件 | 用途 |
| --- | --- |
| `bandwidth.csv`、`bandwidth.md` | 每卡及共同时间窗带宽、memory/backend/方向/状态 |
| `topology.svg`、`topology-overview.svg` | 完整所有 PCI functions 和可读概览，包含 NIC/NVMe |
| `topology-evidence.md`、`commands.md` | 精确父子关系、命令、stdout/stderr、sysfs 读数和证据来源 |
| `report.html` | 离线交互报告：假设、对照、证据、决策、范围和限制 |
| `reproduce.sh`、`cases/`、`reproduce-cases/`、`reproduce-kit/` | Case/实现/执行配置归档；硬件 run 与历史 replay 各按自身协议在新目录复现 |
| `raw/`、`commands.jsonl`、PMU receipts | 原始证据和哈希，不以表格覆盖原始结果 |

空白/unknown/unsupported 不等于零带宽。HD 为 H+D 合计，不能与单向容量混用；group total 使用共同窗口。PMU 平台与单位限制见 [pmu-platforms.md](pmu-platforms.md)。

已验证范围、真实运行与限制见 [validation.md](validation.md)。
