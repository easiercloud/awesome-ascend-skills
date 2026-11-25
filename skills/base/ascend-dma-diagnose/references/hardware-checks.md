# CPU、DDR、链路和背景负载的补证

在相应假设成立时选择这些只读检查；保存命令、UTC 时间、返回码和原始输出到该 run。默认采集器已经记录 CPU/socket/NUMA、PCI 父链、端口 current/max、AER 暴露状态和 governor。额外输出可能包含设备序列号；保留在用户的私有报告，公开前脱敏。

## CPU 频率与内存配置

```bash
lscpu
lscpu -e
numactl --hardware
numastat -m
# 工具不存在时记录 unavailable，沿用脚本的 sysfs/proc 证据，不自动安装。
grep -E '^(processor|physical id|model name|cpu MHz)' /proc/cpuinfo
for p in /sys/devices/system/cpu/cpufreq/policy*; do
  [ -d "$p" ] || continue
  printf '%s\n' "$p"
  for k in affected_cpus scaling_driver scaling_governor scaling_min_freq scaling_max_freq scaling_cur_freq cpuinfo_max_freq; do
    [ ! -r "$p/$k" ] || { printf '%s: ' "$k"; cat "$p/$k"; }
  done
done
# 需要已有的相应权限；固件字段缺失不补造。
dmidecode -t bios
dmidecode -t processor
dmidecode -t memory
```

按 socket 横比频率与内存条的 Locator/Bank Locator、Size、Configured Memory Speed、Rank。相同 DIMM 数量不能证明通道交错正确；固件的插槽字符串也不能直接替代主板通道连线。`scaling_cur_freq` 与 `/proc/cpuinfo` 是采样/驱动视角，不证明持续负载频率、Turbo 或 BIOS 功耗策略。需要负载频率对照时，使用已有的 `turbostat`/厂商工具并保存版本、采样边界；不为此写 MSR。

以系统暴露的实际 node→socket 关系记录 NUMA domains/socket。AMD NPS、Intel SNC 等固件设置需要 BIOS/BMC 导出或对应平台文档佐证；不能只按 node 数给 BIOS 设置命名。内存总量和 VMA 节点分布不能证明当前 DMA buffer 的精确页归属，使用 worker 的逐页查询结果。

BIOS/BMC 只使用用户已有的管理方式和权限读取配置导出；不同厂商 Redfish/BIOS 属性名不同，不编造统一路径。不主动修改 BIOS、重启、改 NPS/通道交错。缺少管理证据时在报告中写 unknown，并优先完成无需这些信息的匹配实验。

## 沿真实 PCI 路径逐跳核验

```bash
bdf=0000:03:00.0  # 替换为本机已核验的 NPU BDF
path=$(readlink -e "/sys/bus/pci/devices/$bdf") || exit 1
while [ -n "$path" ] && [ "$path" != / ]; do
  port=${path##*/}
  if [[ $port =~ ^[[:xdigit:]]{4}:[[:xdigit:]]{2}:[[:xdigit:]]{2}\.[0-7]$ ]]; then
    lspci -D -s "$port" -vv
    for a in "$path"/aer_*; do
      [ ! -r "$a" ] || { printf '%s\n' "$a"; cat "$a"; }
    done
  fi
  path=${path%/*}
done
lspci -Dtv
```

从 endpoint、downstream、upstream 到 Root Port 核验角色、链路两端、当前 speed/width、双方最大能力、链路状态及 AER。拓扑深度可变，不能假定固定三个 `dirname` 就总能得到同一角色。与同类型端口比较并在授权搬运负载前后重采，排除空闲节能降速、布线/分叉和端点能力差异。Switch 内部 upstream→downstream 层级不等同于另一条外部物理 wire。

`lspci -t` 说明连接关系，不显示正在使用的 lane 数；BDF 相近、同 RootBus 或 DSN 相同都不能单独证明共用一条链路。AER 不暴露、无日志或计数没有增长也不足以证明无硬件问题。不要为排查自动执行 `setpci` 写操作、reset、remove/rescan、驱动卸载或重启。

## 背景 CPU、DDR、磁盘与网络

```bash
uptime
vmstat 1 5
iostat -xz 1 5
cat /proc/meminfo
cat /proc/pressure/cpu /proc/pressure/memory /proc/pressure/io
ip -s link
```

只使用已有命令。对照实验期间将这些观测与 DMA 时间窗对应，区分 CPU 竞争、内存压力、swap、NVMe/网络背景 I/O。空闲 NPU 不等同于整机 CPU/DDR/I/O 空闲。不能把全机流量直接归给当前进程，也不能据磁盘厂商名判断 CPU 内存控制器性能。

优先匹配单卡本地/远端 CPU、精确页归属、不同 Root Port/CPU I/O 单元、相反方向和 torch/ACL。只改一个变量，保留负结果。CPU/DDR PMU 用[型号门控适配器](pmu-platforms.md)；没有已验证事件时不拿其他 CPU 的 raw 编码试套。
