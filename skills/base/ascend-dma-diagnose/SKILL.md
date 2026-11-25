---
name: ascend-dma-diagnose
description: "昇腾 NPU 单卡、多卡及多主机的 pinned/pageable H2D、D2H、双向 DMA 带宽测试和降速归因。联查完整 PCIe、CPU/NUMA、IIO/IOM 与框架代码，自动设计匹配对照并交付带宽表、拓扑和证据报告；不替代 HCCL 集合通信测试。"
---

# Ascend DMA 带宽与拓扑诊断

用户请求测试或排查主机↔NPU 搬运性能时，完成采集、基准、异常对照和报告。一次请求足以启动授权机器与设备范围内的整套流程，不要求用户为每个常规对照重写 prompt。用户只要求解释已有输出时，先分析已有证据，保持其只读范围。

## 快速开始

在 skill 目录、目标机器的宿主机上运行：

```bash
python3 scripts/dma.py run --out runs/dma --cards all \
  --memory pinned pageable --directions H2D D2H HD \
  --modes solo concurrent --backend torch --diagnose --performance require
```

按机器实际情况附加 `--env-script /path/to/set_env.sh`、`--container existing-name` 或用户提供的 `--source-code PATH`。默认只使用已有环境；连接方式沿用项目规则或已授权的 SSH 配置，不保存认证信息。安装、单卡、重跑和完整参数见 [usage.md](references/usage.md)。

## 诊断工作流

1. **确认可测试范围。** 发现实际设备数和 runtime logical ID↔physical ID↔BDF。多芯片、非连续物理 ID、部分可见或重排无法确证时，要求明确的 device map，禁止按 BDF 排序造 rank。区分整机设备数与本次选择数。
2. **先采完整证据。** 包含所有 PCI functions、NPU、NIC、NVMe、其他设备，记录真实 parent 链、RootBus、Root Port、链路两端 current/max、AER、DSN、CPU socket/NUMA。一个 CPU 可有多个 NUMA node，不能拿 node ID 当 socket ID。
3. **在空闲机器做完整基准。** 环境不明确时先用 `doctor --load-backend torch`（或 `acl`）核验已有运行库，使用同样的 container/env-script。默认 pinned/pageable × H2D/D2H/HD × 逐设备 solo/选中设备 concurrent。用户限制的设备、内存类型或方向优先。训练忙则等待或换已授权空闲机器，不结束训练、不重启容器来腾资源。
4. **异常后主动定维对照。** 用同一上游对卡同向/反向、独立上游同向、各设备双向、CPU/NUMA 绑定和 torch/ACL 匹配对照缩小原因。每轮只改变一个关键维度。CLI 默认诊断预算 12 个追加 case；仍缺关键证据时在授权范围内调整预算或追加独立 case，不把预算耗尽当成根因已确认。
5. **联结计数器和代码。** CPU/DDR/BIOS、逐跳链路和背景负载补证按 [hardware-checks.md](references/hardware-checks.md)；需要 IIO/IOM/IMC 时读 [pmu-platforms.md](references/pmu-platforms.md)；需要解释框架与底层 API 差异时读 [code-paths.md](references/code-paths.md)。没有型号适配、权限或源码时保留未知，不能补造解释。
6. **完成证据归因与交付。** 每项结论列出假设、若真/若假的预期、实验、原始结果、决策及限制。受控对照支持到哪一级就写到哪一级，未定位的内部资源/调度层级明确保留。

详细的触发条件、实验选择和停止条件见 [diagnostic-playbook.md](references/diagnostic-playbook.md)。多机统一采集和跨主机对照按 [fleet.md](references/fleet.md) 执行。维护或接入脚本时再读 [data-contract.md](references/data-contract.md)。

## 必须保持的测量口径

- 主控制器在 spawn worker 前不导入 NPU 框架。分配、first touch、warmup 和内容验证在计时外；同一 repeat 的所有选中设备重复 barrier，核验完整样本和内容。
- HD 使用独立 H/D 缓冲区；pinned 为两个设备 stream，pageable 同步 API 用两个 host 线程。HD 的字节数是 H+D 各计一次，不能把串行 H 再 D 改名为双向并发。
- GB/s 为十进制。每卡按自己的完成时间计算；group total 按共同时间窗计算，不直接相加每卡中位数。Pageable 同步 enqueue 已包含复制完成与 HD 线程 join；Event 包含 stream 等待和提交间隙，不能叫纯 DMA engine busy time。
- 对照保持 CPU/NUMA、backend、张量 dtype/layout、copy/sync 协议和软件版本一致。Pinned 页查询 EFAULT、容器限制或整段 VMA 信息都不能证明精确缓冲区已落在本地 NUMA。
- 按实际 PCIe 代际、协商宽度和两端能力计算容量，不使用跨代、跨宽度的固定带宽阈值。Pageable 包含 staging/CPU 路径，不与 pinned 的 PCIe 理论值作硬性比较。
- 同 RootBus 不等于同一条共享链路；DSN 重复不确定物理 ASIC；IOMMU group 不等于 IOM/IIO。拓扑中的共同路径只是观测事实，其降速因果需要匹配实验。
- PMU unsupported/未计数不是零；方向标签按该型号事件语义保存，再用定向流量校准。计数率或一次平台值不等于 IOM/IIO 上限，双向合计也不等于单向容量。

## CPU 性能模式

`--performance record` 只记录，`require` 核验现有 governor，`set` 在本轮修改并恢复 governor。用户明确要求高性能时，使用 `set` 或以 `require` 证明已满足；不能只写“已优化”。这不代表 BIOS/BMC 高性能、DDR/NPS 或内存通道配置已核验，缺少的硬件证据单独列出。

## 三组交付

1. `bandwidth.csv`、`bandwidth.md`，附可执行复现命令、case 参数和脚本归档，区分完整、失败、不支持和中断。
2. 清晰可读的 `topology.svg`、概览图及 `topology-evidence.md`；包括 NIC/NVMe，概览折叠公开精确成员，完整树不丢节点，命令可追到 stdout/stderr 与哈希。
3. 离线 `report.html`：怀疑 → 区分性实验 → 证据 → 决策 → 限制；结论分 measured/derived/inferred/unknown。仓内 replay 为真实历史脱敏数据，保留 historical_replay 标记，不能写成本轮新实测；模板或合成测试不作为硬件证据。

交付前检查图中节点/父子边与采集数据一致、CSV 与原始 sample 可重算、复现记录不含口令或真实认证信息。源码候选命中、硬件配置未读到、PMU 不支持等限制保留在最终报告。
