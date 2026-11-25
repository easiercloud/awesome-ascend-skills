# CPU I/O 计数器的适用范围

PMU 用于验证流量经过哪个单元、流量是否随受控实验变化。计数器观测值、PCIe 编码容量和设备实测有效带宽是三类不同数据。某次达到的峰值不能直接命名为 IIO/IOM 的硬件带宽上限。

## 平台门控

| CPU | 自动采集 | 计数单位 | 其他条件 |
| --- | --- | --- | --- |
| Intel GenuineIntel Family 6 Model 173（GNR） | IIO CPU memory read/write；IMC 两个 subchannel 的 CAS read/write | IIO 每 count 4 B；IMC 每 count 64 B | PMU 名称和 sysfs format 均通过检查；host 有 perf 及相应权限 |
| AMD AuthenticAMD Family 19h Model 11h（十进制 25/17，Genoa） | DF IOM0–3 的 local/remote read/write | 既有型号适配器使用 64 B/count | 必须是下述 PerfMonV2 format；仍需单设备定向流量校准 |
| 其他 Intel/AMD、Arm、虚拟化或型号不可读 | 只发现 PMU、格式、符号事件、scale/unit 和已安装工具 | 不推测 | 不复用以上 raw 编码；保留 unsupported/unavailable 状态 |

不自动安装 PCM、AMD uProf、驱动或内核模块。已安装的 `pcm-iio`、`pcm-memory`、`AMDuProfPcm` 只记录可用路径；没有经过适配的 CPU 不会自动运行这些工具或复用另一型号的编码。权限不足不等同于零流量。

## Intel GNR：RootBus → IIO

静态映射复现 [Intel PCM 的 BirchStreamPlatform](https://raw.githubusercontent.com/intel/pcm/master/src/pcm-iio-topology.cpp)，并按 [get_cpu_bus](https://raw.githubusercontent.com/intel/pcm/master/src/utils.cpp) 和 [寄存器常量](https://raw.githubusercontent.com/intel/pcm/master/src/types.h) 读取配置空间。所有配置访问均为 `O_RDONLY` 加 `pread`，不写 PCI/MSR，不配置计数器。

1. 枚举 sysfs 中 vendor/device 为 `8086:09a6` 的 MSM。
2. 读取 MSM `0x1a0` 的 CPUBUSNO_VALID。
3. 读取 `0x190,0x194,0x198,0x19c,0x1c0,0x1c4,0x1c8,0x1cc` 八个 DWORD。有效位的索引是 SAD CPU bus ID，对应 DWORD 字节给出 PCI bus number。
4. 在第一个有效 bus 的 `domain:bus:00.0` 读取 `0x3f4`，低四位给出 package/socket。
5. 以 SAD ID 查 PCM 的 IIO PMON 表；禁止按 bus 大小或 BDF 排序编号。

例如 SAD `1,2,3,4` 分别对应 IIO `2,3,4,5`。SAD `26/29` 存在普通 PCIe 与 FlexUPI 两套别名；本实现保留候选、将唯一 unit_id 留空，不假定物理配置。整个 socket 表读失败会保留错误并将状态标记 partial/unavailable。DSN 不参与 IIO 映射，也不用于合并 switch ASIC。

每个 PCI function 的实际 sysfs parent 决定路径。RootBus 与 Root Port 是不同层级；同 RootBus 的不同 Root Port 不代表共享同一条物理 PCIe 链路。

## Intel GNR：事件和单位

来源为 [Intel GNR uncore events](https://raw.githubusercontent.com/intel/perfmon/main/GNR/events/graniterapids_uncore.json) 和 [GNR metrics](https://raw.githubusercontent.com/intel/perfmon/main/GNR/metrics/graniterapids_metrics.json)。不把 Free Running `bw_in_portN` 的 N 自动当成 IIO 编号。

IIO `UNC_IIO_DATA_REQ_OF_CPU.MEM_READ.ALL_PARTS` 和 `.MEM_WRITE.ALL_PARTS` 使用 `event=0x83`，umask 分别 `0x04/0x01`，`ch_mask=0xff,fc_mask=0x7`。模型匹配之后，还必须检查 `event=config:0-7`、`umask=config:8-15`、`ch_mask=config:36-47`、`fc_mask=config:48-50`。每次计数为 4 字节；读写名称保留事件语义，实验的 H2D/D2H 仍按观测流量关联。

IMC subchannel 0/1 分别 `event=0x05/0x06`，read/write 分别 `umask=0xcf/0xf0`。检查 `event=config:0-7`，umask 为 `config:8-15` 或 `config:8-15,32-55`。每 count 为 64 B。只使用 kernel 的单个 `cas_count_read` 别名可能只覆盖一个 subchannel；适配器明确采集两个 subchannel，且不重复累加别名与 raw 事件。

每个 PMU 的 cpumask 与 CPU→socket 元数据一起保存。相同 IIO 编号在两个 socket 上是两个实例，不能跨 socket 合并为一个单元。

## AMD Genoa：经过验证的窄适配器

这一适配器沿用 Family 19h Model 11h 的已验证 DF IOM 编码，不宣称适用于所有 Zen 4/EPYC。IOM i 的 event 为 `0x81f + i*0x40`；local read/write 的 umask 为 `0x7fe/0x7ff`，remote read/write 为 `0xbfe/0xbff`，换算单位为 64 B/count。必须核对运行机器对应的 PPR/stepping，并用单设备测试验证字节量与方向。对应的 AMD PPR 为 [55901 官方入口](https://docs.amd.com/v/u/en-US/55901)；这里提供核对入口，不声称已由此验证所有事件语义。格式编码依据 [Linux AMD uncore 驱动](https://raw.githubusercontent.com/torvalds/linux/master/arch/x86/events/amd/uncore.c) 的 PerfMonV2 定义；该驱动证明 format 布局，不单独证明全部事件语义。

仅接受：

```text
event = config:0-7,32-37
umask = config:8-15,24-27
```

`local_write` 等字段保留原始事件名称，不能直接硬译成 D2H。适配器没有依据 RootBus 的数字推断 IOM；必须通过单卡、单方向的 dominant event/字节量校准。多卡出现多个活跃路径时，不给每个 rank 随意分配一个 IOM。

## 实际采集与时间解释

`discover(topology)` 只读取元数据；`start(out,topology,label)` 在 host 启动独立的 `perf stat`；`stop(handle)` 仅向 handle 中自己的 Popen 发 SIGINT，必要时只终止同一进程。不会 `pkill perf`，不会向无关进程组发信号。

采集命令使用 `-a -A -I 200 --no-scale -x ';' --log-fd 1`。原始 stdout/stderr、argv、时间、返回码和 SHA256 都保留；discovery JSON 保存 PMU type、format、符号事件及其 `.scale/.unit`。读取到的 clockticks 别名仅作为单位证据，不根据名称猜时钟频率。

每行结果保存 count、实际单位、interval 时间、perf runtime、running percentage、推导 enabled/running 时间和缩放。raw 事件的换算为：

```text
scaled_bytes = raw_count * 100 / running_percent * verified_bytes_per_count
GBs = scaled_bytes / interval_seconds / 1e9
```

如果 perf 明确输出 MiB 等已缩放单位，按该单位换算，不再盲乘 count 的字节数。`<not counted>`、`<not supported>`、零 running time、未知单位分别保留未知值和原因，不转成零带宽。

`summarize(receipt_path)` 的 `metrics` 提供 cpu/socket、unit_kind/unit_id、direction_label、whole_run_count/whole_run_bytes、running_pct、peak_GBs、coverage_complete、status 和 alignment。出现缺测区间时，累计量仅覆盖有效区间，status=partial；不能把它当成完整一轮 DMA 字节量。

观察窗口包含计数器启动、分配、warmup、内容验证、间隙和背景 I/O。200 ms bins 不等同于 DMA repeat 边界；running time 也不是传输 wall time。必须记录时间不确定性，用原始 benchmark 时间窗作对齐判断，不能将整个 perf run 的平均速率当成该 case 的精确传输效率。
