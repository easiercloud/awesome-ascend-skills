# DMA 降速诊断手册

适用于已授权目标机器上的 Host↔NPU 传输。收到“帮我测并排查”后自主完成闭环；只解释已有日志的请求保持只读。命令见 [usage.md](usage.md)，结果格式见 [data-contract.md](data-contract.md)。

## 先把身份和测量条件固定下来

采集所有 PCI functions，而非只抓 NPU 路径。记录 runtime logical ID、physical/chip ID、BDF 和映射来源。单芯片、连续物理 ID、完整且不重排的可见性均已验证时才能用 identity mapping；其余情况需要显式 device map。不能把“可见八设备”写成十六设备机器的“整机全卡”。

路径来自实际 sysfs 祖先。分别画 RootBus、Root Port、switch 上下行端口和端点；包含 NIC、NVMe、其他 PCI 设备。RootBus 是 Linux 的逻辑根；多个 Root Port 可归属同一 RootBus，却各有物理链路。跨过哪个 switch 内部层级不能凭 BDF 数值相邻补线。DSN 作为原始字段保留，绝不据此合并 ASIC。

CPU 元数据包括逻辑 CPU→socket、每个 NUMA node 的 CPU 集合与内存量。多个 node 可属于同一 socket；设备 `numa_node=-1` 代表未知。CPU governor、频率快照和 CPU 型号分别保存。BIOS 高性能模式、DIMM 数量、通道布局、NPS 配置只有读到相应 BMC/固件/系统证据才能确认，不能由 governor=performance 代替。

每个 case 开始前检查空闲状态。已有训练占用时等待或选择用户已授权的空闲机器，保留等待原因；不能结束训练、重置 NPU 或改容器资源来腾空。远程地址/连接方式确实缺失时才向用户索取必要信息，不为每个普通对照增加审批。

## 第一轮完整矩阵

默认测量 pinned/pageable × H2D/D2H/HD × solo/concurrent。Solo 逐一覆盖选中设备；concurrent 同时覆盖同一选择集。用户给定的子集、方向、内存类型和 backend 优先。默认 torch；ACL 用作相同条件的匹配佐证，不静默替代失败的 torch case。

每轮固定 buffer 字节数、迭代数、warmup、repeat、CPU/内存策略、CANN/driver/torch/torch_npu、环境和可见性。CPU 高性能请求必须设置或核验；修改 governor 只在空闲测试范围内进行并恢复。没有得到精确 pinned 页位置时标记 unknown，即使 CPU affinity 已生效也不等同于内存已本地化。

核验下列可重算关系：

```text
bytes = MiB * 2^20 * iters * (2 if HD else 1)
per_card_GBs = bytes / own_wall_seconds / 1e9
group_GBs = sum(bytes) / (max(end) - min(start)) / 1e9
```

Group total 不等于每卡速率中位数之和。Mixed 方向的两卡完成时间可以不同；共同时间窗中的方向速率，也不同于该方向设备自己完成时的速率。HD 为两方向字节之和，比较时须统一这个口径。

Pageable 包含主机准备、staging、同步和 CPU 内存路径，低于 pinned 并不直接说明链路降速。Pinned 的 async 请求也不保证两方向在硬件上完全重叠。异常以本设备匹配 solo、多次重复的稳定性、路径分组模式和对照结果识别，不使用一个固定 GB/s 门槛。

## 根据异常选择最小区分性实验

以下“同路径”必须由真实祖先或经过验证的 CPU I/O 映射定义。卡号只是选择实验对象的标签，不预设某两个号天然一组。

| 怀疑 | 若为真应出现 | 首选对照 | 什么结果会削弱该解释 |
| --- | --- | --- | --- |
| 可见 PCIe 上行被多个设备同向共享 | 同向总量出现平台，单卡下降；换独立上行恢复 | 同上行对卡 H+H、D+D；同 CPU 的独立上行对卡相同方向 | 独立上行也同幅下降；平台与上行编码容量和方向行为不相符 |
| CPU 内部 IOM/IIO/内存路径竞争 | 多个独立 PCIe 上行仍随同一 CPU I/O 分组出现稳定平台 | 型号门控 PMU；单卡定向校准；同组/异组、同 socket/异 socket 匹配对照 | 换组不恢复；实际只共享外部上行；计数器不支持或无法可靠归属 |
| 双工执行或方向耦合 | 同向与反向表现不同；每设备 HD 低于理想两方向叠加 | 同对卡 H+D 与 D+H、两卡各做 HD、独立上行两卡各做 HD、单卡 HD | 只换 CPU/内存就恢复，或换 API 后消失，不能单归硬件 |
| CPU affinity / Host 内存落点 | 固定 CPU 或可验证本地/远端内存后有可重复变化 | 保持路径与 backend，仅改变 CPU node 或 memory node | 页位置不可验证、内存绑定失败或同时改了多项条件 |
| CPU/DDR 配置 | 性能随频率策略、socket 或内存布局变化 | 对齐 governor，记录频率；读到的 DIMM/通道/NPS/BMC 配置横比 | 只在同一共享上行的卡对下降，独立上行在同 CPU 下恢复 |
| Python/框架提交或同步 | Pinned 异步 enqueue 很长或有意外同步、转换；pageable 同步须分离 API 内传输 | Enqueue/sync wall 与 CPU time；stream Events；同内存协议 ACL 对照 | 异步提交很快、等待占主导且独立路径恢复，不能归为 Python 循环慢 |
| 张量布局、分配、缓存或 API 协议不同 | 连续预分配相同 dtype 的控制测试恢复 | 查真实代码和版本；只改 layout、allocation 或同步中的一项 | 两 API 条件未匹配、pageable 被替成 pinned、实际传输字节变了 |

每个实验先写“若真/若假”的预期，再选择设备和方向，避免事后只挑符合猜测的数据。默认追加预算 12 个 case；关键证据不足时，可在既有授权范围内调整预算或建立新的独立 run-case，保持原始结果，不需要用户重新发起每一步。不要扩展到未授权设备或机器。

## 如何解释链路与 PMU

Gen1–2 编码效率为 8/10，Gen3–5 为 128/130。编码后的单向容量计算为 `GT/s × lanes × 编码效率 / 8`；这是 packet 开销前的容量，不是应用必然达到的数值。使用当次链路两端 current/max、布线/分叉与横向同类端口判断；一端低于自己的最大能力不自动证明故障。Gen6 及未知模式不外推旧公式。

同向平台、反向恢复与独立上游恢复，可以一起定位竞争边界。如果同一个 IIO 上 H+D 合计已经显著高于同向平台，便不能称该 IIO“所有方向总量固定封顶”。每设备 HD 的低效也可能包含设备内部资源、Runtime 调度或协议耦合；不能只凭双向总量决定具体硬件内部层级。

IIO/IOM 归属按 [pmu-platforms.md](pmu-platforms.md) 处理：Intel GNR 可只读寄存器建静态映射；AMD 仅在被验证的型号/format 上用单设备、单方向流量校准。不用 IOMMU group 序号替代 CPU I/O 单元。平台缺少 PMU/寄存器适配时，可把同 RootBus 的不同 Root Port 对卡作为 CPU 路径候选，并与异 RootBus、同 socket 对照；这种候选或相关性不能改名为共享 wire 或某个 IOM/IIO。不从任意 CPU 的 `perf list` 名称套另一型号的编码。

PMU 校准仅采纳有效 observer receipt、完整覆盖及可解释 running ratio 的指标。累计字节包含分配、warmup、验证与背景，需结合真实时钟与重复区间判断。多卡同时激活多个单元时，保留“这组流量经过这些单元”，不能逐 rank 按顺序分配。事件名中的 read/write 保留型号语义，不直接改名 H2D/D2H。

Stream Event 的时间包含队列等待与 host 提交间隙；在 pinned 异步协议下，enqueue 短且 sync 长只能削弱“提交循环慢”的解释，不能证明某个 DMA engine 本身饱和。Pageable 同步协议的 enqueue 已包含传输完成与 HD 两线程 join，不能直接解释为 Python 提交开销。精确内部原因需要版本对应的调度/源码/Profiler 证据，读不到就停留在“设备或 Runtime 完成路径”。

## 结束条件与三组交付

结束一轮诊断的条件是：基准完整或缺项已明确；主要假设有匹配对照支持/反证；能解释稳定的卡对/方向/总量模式；其他层级仍未确定的部分如实保留。不要求穷举全部组合，也不因预算到头将 hypothesis 改成 confirmed。

1. 带宽表保留 memory、backend、mode、card/BDF、direction、CPU/NUMA、每卡与 group rate、验证和失败状态；附命令、case 文件及复现脚本归档。
2. 完整拓扑图包含全部 PCI functions；概览折叠公开精确成员，标清关联与物理相邻边。`topology-evidence.md` 将每个结论追到命令、原始 stdout/stderr、sysfs 读取、偏移和哈希。
3. HTML 逐项展示怀疑、预测、实验、原始证据、决策、范围、限制和下一步。Measured、derived、inferred、unknown 分开；合成测试、未完成和 unsupported 不能混入成功测量。仓内 replay 是真实历史脱敏测量，单独保留 historical_replay 来源与公开证据缺失边界，不能计作本轮新测。

移到新机器时重新采集设备图和映射，不复用旧 BDF。最终回答明确选择集、三方向结果、最强因果证据、未解决的内部层级和报告入口。
