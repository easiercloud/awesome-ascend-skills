# 多机同步测试本地 DMA

`fleet` 协调多台主机在同一轮启动各自的 CPU↔NPU 数据传输。每台主机保留自己的 CPU、PCIe、NPU、网卡和 NVMe 拓扑，卡号、BDF 和带宽都带 `host_id`。这项测试没有传输跨主机数据，因此不能用于判断 RDMA 或 HCCL 的网络带宽。

## 配置与启动

连接使用已有 SSH 配置或已经认证的包装命令。默认 SSH 参数包含 `BatchMode=yes` 与 `StrictHostKeyChecking=yes`；先在用户终端完成公钥认证和主机密钥登记。配置不接受密码或私钥内容。

```json
{
  "hosts": [
    {
      "id": "host-a",
      "ssh": "npu-host-a",
      "workdir": "/tmp/ascend-dma",
      "env_script": "/usr/local/Ascend/cann/set_env.sh",
      "cards": [0, 1, 2, 3]
    },
    {
      "id": "host-b",
      "ssh": "npu-host-b",
      "workdir": "/tmp/ascend-dma",
      "container": "existing-npu-container",
      "env_script": "/usr/local/Ascend/cann/set_env.sh",
      "cards": [0, 1]
    }
  ]
}
```

`cards` 使用该主机的运行时逻辑编号，可以各不相同。省略时选择采集到的逻辑卡。所有选定卡仍须具备已核验的逻辑号→物理号→BDF 映射；发现工具不能确认时，可在主机条目中提供 `device_map` 列表，格式见[数据契约](data-contract.md)。不会根据编号或相邻 BDF 猜测映射。

```bash
python3 scripts/dma.py fleet \
  --hosts hosts.json --out results-fleet \
  --memory pinned pageable --directions H2D D2H HD \
  --mib 256 --iters 32 --warmup 3 --repeats 5
```

默认先运行每台主机、每卡、每种内存、每个方向的单卡基线，再逐轮同步多卡测试。只需要并发部分时加 `--skip-solo`。使用 `--backend acl` 对照裸 AscendCL；它直接加载现有 CANN 动态库，不安装软件或编译代码。

```bash
python3 scripts/dma.py fleet \
  --hosts hosts.json --out results-fleet-pinned \
  --memory pinned --directions HD --skip-solo \
  --backend torch --event-timing --repeats 5
```

Pageable HD 使用两个主机线程分别执行同步 H2D、D2H；pinned HD 使用独立缓冲与两条设备 stream。协议随每卡结果保存。ACL pageable 同步 API 没有设备 stream 事件计时语义，此组合请求 `--event-timing` 会明确标为 unsupported；默认仍记录提交、同步及 CPU 耗时。

CPU governor 默认只记录。指定 `--performance set` 才会临时设置，并在每个 case 的 finally 路径恢复。`--performance require` 要求当前已为 performance。`--cpu-node` 与 `--memory-node` 分别申请 CPU 和内存 NUMA 绑定；驱动 pinned 内存逐页查询返回 EFAULT 时，严格内存绑定会标为不可验证，不会报告成功。

## 连接包装器

`transport_argv` 替代 SSH 命令，需要接收标准输入中的 bash 脚本，并保留其退出码和标准输出。例如已有的受控跳板工具：

```json
{
  "id": "host-through-wrapper",
  "transport_argv": ["/path/to/welm", "host", "--stdin"],
  "workdir": "/tmp/ascend-dma",
  "python": "python3"
}
```

这些参数逐项传给 `subprocess`，不拼成连接命令字符串。包装器必须已经配置好认证；不能通过这里传入明文密码。`python` 指远端主机上可执行的 Python；指定容器时，默认的 `python3` 也须能在容器环境中使用。

## 生命周期与取消

控制器只打包本 skill 的 `scripts/`、`assets/`，排除字节码缓存；每次在各主机创建新的唯一目录。解包前检查所有 tar 成员，拒绝绝对路径、`..`、重复文件、软硬链接及设备文件，不覆盖已有目录。

每台主机独立采集拓扑并完成基线。基线执行器关闭、CPU 策略恢复后，节点写 `baseline_complete` 等待控制器的 `go`，此时不持有测量执行器。所有节点进入并发阶段后，每个 repeat 都执行：

1. 所有参与卡完成准备，节点写入匹配 token、case ID、repeat 的 `ready`。
2. 控制器等所有主机准备好，发送校正到各自主机时钟的未来 epoch。
3. 各主机等待自己的释放时刻，再由本机卡间 barrier 启动复制。
4. 所有卡的该轮样本完成后，节点进入下一轮 ready。

控制器不会恢复旧 gate。单卡基线使用本机预释放的独立 gate，以便接收同一种取消标记，不受跨机 repeat barrier 限制。

发现忙碌、失败、超时或控制器中断时，只向本次唯一目录写带 token 的取消标记。节点执行器负责终止自己的测量进程并恢复策略；不调用 `killall`，不终止训练进程。控制器等待节点清理确认再取回结果。连接中断导致无法确认清理时会保留错误，节点自身的 case/control timeout 仍然生效。

## 校时与结果

每次 SSH 校时保留控制器请求前后时间、远端时间、往返时延、midpoint offset 和 `RTT/2` 误差界。选择最短 RTT；完成较长基线后，在每个并发 case 前重新校时。校时后主机时钟漂移没有被持续观测，因此记录的起跑差属于带校时误差的主机计时估计，不是跨设备硬件时间戳证明。

```text
results-fleet/
├── fleet.json
├── report.html
├── bandwidth.csv
└── hosts/
    ├── host-a/report.html
    ├── host-a/topology.json
    ├── host-a/results.json
    └── host-b/...
```

每个成功结果取回后重新核对原始 JSONL、校验记录与 SHA256。CSV 按主机、case、卡分别列带宽；`host_case_total_GBs` 只表示该台主机的组时窗带宽，不能把逐卡重复出现的这个字段再次求和。

`fleet.json` 保存逐轮释放时刻、各主机校时、样本换算后的起跑差和误差界。缺少主机、repeat 或足够校时精度时标为 `unsynchronized`。`--max-start-skew-ms` 与 `--max-clock-error-ms` 默认均为 100 ms，可以收紧；它们不改变原始数据。根报告始终保留各主机本地结果，不生成跨主机“全局带宽”或把相同 BDF 合并成一棵 PCIe 树。

内部 `_fleet-node`、`_fleet-control` 命令只用于协调器协议，不是绕过主机准入和设备互斥的入口。控制请求必须匹配该次 run token，文件操作限定在该 run 目录内。

## 跳板开销过大时

SSH midpoint 的误差界包括连接包装器、进程启动和往返开销。估计起跑差很小，也不能抹掉这个界：若包装器每次耗时接近一秒，短 DMA 的真实跨机重叠仍可能无法证明，报告应保留 `unsynchronized`。每台主机已验证的每卡带宽仍可使用；不因此生成一个“全局同步总带宽”。

需要缩小误差时，在已授权、靠近被测节点的机器上运行协调器，沿用已有认证，不为得到通过状态随意放宽阈值。协调器所在机器也参加测试时，该 host 条目可使用：

```json
{
  "id": "coordinator-host",
  "transport_argv": ["bash", "-s"],
  "workdir": "/tmp/ascend-dma",
  "env_script": "/usr/local/Ascend/cann/set_env.sh",
  "cards": [0, 1]
}
```

其他主机使用该协调器上已有的 SSH alias/控制连接。保留客户端与服务器侧两轮原始校时、阈值和结果，不能事后改旧报告来伪造同步证明。源代码和原始输出含实际连接/机器信息时只保留在用户私有归档。

## 起跑对齐和循环重叠分别验收

`loop_window_overlap` 每轮先取每台主机全部请求卡的交集 `[max(start), min(end)]`，按 clock offset 转为协调器时间，再计算 `min(end-error) - max(start+error)`。全部请求主机、卡、repeat 完整且每轮保守下界大于零时，顶层 `overlap_status=proven_loop_window_overlap`；缺测或未知误差保留 unproven。它与起跑对齐门槛分别展示，不修改 `synchronization_status`。这些是包含提交/等待的 host 计时循环窗口；校时后的漂移未持续观测，结果以记录的误差界为条件，不证明 engine busy 重叠。

新运行的 `controller-invocation.json`、`hosts-config.json` 和 parameters 保存原始入口、规范主机配置及调度/同步阈值。复现时用新的输出目录与本轮新生成的 gate/token，保留旧归档；原始连接信息按私有证据管理。
