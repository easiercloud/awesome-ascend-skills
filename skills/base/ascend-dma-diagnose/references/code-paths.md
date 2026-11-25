# 沿用户代码追踪搬运路径

当硬件路径对照不足以解释差异，或 torch 与 ACL、不同项目/版本的结果不一致时使用。先保留对应基准，不把源码里的一次关键词命中当成真正执行过的路径。

## 固定版本与最小边界

记录 Python、torch、torch_npu、CANN、driver、设备型号、加载的 Runtime 库与环境脚本。安装包路径可以先不导入框架地查询：

```bash
python3 - <<'PY'
from importlib import metadata
for name in ('torch', 'torch-npu'):
    try:
        dist = metadata.distribution(name)
        print(name, dist.version, dist.locate_file(''))
    except metadata.PackageNotFoundError:
        print(name, 'not installed in this environment')
PY
git -C /path/to/version-matched-source rev-parse HEAD
git -C /path/to/version-matched-source status --short
```

只读取用户选择的源码目录。默认 `--source-code PATH` 的扫描有文件数和命中数上限；命中记录带文件、行号、哈希和 candidate 状态。若提示截断，收窄到实际 benchmark、copy kernel、allocator 或 stream 调度目录继续检查，不把“没扫到”写成“不存在”。

主控制器在 spawn worker 前不导入 torch/torch_npu。需要查询 dispatch table 或做 Profiler 时，在空闲机器的独立诊断子进程中执行，沿用该 case 的环境、可见性和设备映射。

## 从应用到 Runtime 分层核对

| 层级 | 要核对的事实 | 证据如何获得 |
| --- | --- | --- |
| 用户 benchmark | `.to()` 还是预分配 `.copy_()`；是否在循环里分配、pin、同步、转换；真正传输多少字节 | 实际调用行、计时边界、buffer size、迭代及内容验证 |
| 张量与分配器 | dtype、shape、stride、storage offset、contiguous、内存 format、是否 pinned、first touch | worker 元数据/最小复现；不能只看变量名或配置项 |
| PyTorch dispatch | `aten::copy_` / `_copy_from` 被哪个 backend 注册并实际接管 | 与安装版本一致的源码和 dispatch 输出；不能假定通用 CPU/CUDA 路径就是 NPU 路径 |
| torch_npu / op-plugin | copy、allocator、格式转换、队列提交、current stream、额外同步 | 对应提交源码的文件行号；必要时以 Profiler/API trace 佐证 |
| CANN Runtime | 同步/异步 memcpy、参数字节数、方向、stream/context 和内存类型契约 | 已安装头文件/同版本官方 API、加载库和实际调用记录 |
| 设备与 PCIe/CPU I/O | 调度、等待、路径竞争是否随受控对照改变 | Benchmark wall/Events、拓扑、型号门控 PMU，不靠函数名补造硬件层级 |

可以从窄目录查询候选：

```bash
rg -n 'copy_|_copy_from|aclrtMemcpy|aclrtMemcpyAsync' /path/to/copy-source
rg -n 'pin_memory|aclrtMallocHost|aclrtHostRegister|contiguous|format_cast' /path/to/allocator-or-benchmark
rg -n 'synchronize|wait_event|wait_stream|current_stream|aclrtSetCurrentContext' /path/to/stream-source
```

[PyTorch 的 Copy.cpp](https://github.com/pytorch/pytorch/blob/main/aten/src/ATen/native/Copy.cpp) 展示通用 copy 与 backend redispatch，但 main 分支只能作为导航，不能证明用户已安装版本的行为。NPU 调度应查相应版本的 [Ascend/pytorch](https://github.com/Ascend/pytorch) 与 [Ascend/op-plugin](https://github.com/Ascend/op-plugin)，先定位真实文件，再引用文件与行号；不要编造固定的 CopyKernel 文件路径。闭源 Runtime 内部未获取到的部分标记 unknown。

## 设计匹配的软件路径对照

默认基线为预分配、连续、相同 dtype 的 byte buffer，分配/warmup/验证均在计时外。与用户代码比较时逐项保持字节数、内存类型、设备、CPU/NUMA、迭代、stream、同步、版本一致；每次只改变一个待验证维度。

- 用户用 `.to()` 时，先区分其分配/转换成本与真正传输；以预分配 `.copy_()` 对照，保留差异边界。
- 用户张量非连续或 format 不同时，连续 byte buffer 是控制组，不是同一软件路径；若恢复，继续分离 contiguous/format/dtype 的影响。
- Pinned 与 pageable 分开。不能把 pageable 复制成 pinned 后仍标为 pageable，不能把同步接口串行 H 再 D 命名为 HD。
- Torch 的 pinned 基准为 `copy_(..., non_blocking=True)` 加两个独立 stream；ACL 对照为 `aclrtMemcpyAsync` 加独立 stream。请求异步不等于已证明完全重叠。
- Pageable 同步拷贝的 HD 用两个 host 线程分别发起 H/D；每个线程有正确的 NPU device/context，通过线程 barrier 放行并在返回前 join。Torch 的 blocking copy 仍选择对应 current stream；ACL `aclrtMemcpy` 不附着于 stream，不能凭 `streams=2` 说成两条异步流。ACL synchronous pageable 的 Event 请求明确返回 unsupported；未请求 Event 时使用真实 host wall 数据。
- 保持 H/D 使用独立 source/destination，避免先写后读同一 buffer 引入数据依赖；字节数分别计入 H+D。

如果 ACL 和 torch 同向对照都随同一上游分组下降，软件入口不再是首要解释，但也不能据此确定 CPU 内部哪个 buffer/credit/engine。若仅某入口异常，查看其提交、allocator、格式和同步差异，再做更窄对照。禁止静默换 backend 后把结果写成原 backend 已正常。

## 计时解释与结论边界

Host enqueue wall、enqueue CPU、final sync wall、process CPU 和 stream Event 回答的问题不同。Event 从该 stream 的首个 marker 到最后 marker，包含队列等待、依赖和提交间隙；它不是“纯 engine busy”。

对于 pinned 异步协议，enqueue 很短、sync 很长，支持“瓶颈在提交后完成路径”，不足以指定硬件引擎。Pageable 同步协议的 enqueue 已包含复制完成，HD 还包含两个 host 线程 join，不能用同一解读把它全部归为框架提交慢。H2D Event 较短、D2H 较长，支持方向执行不均衡；不自动证明只存在一个 DMA engine、D2H 完全串行或某个 IIO 双向硬封顶。两个方向 Event 的和也不能作为 wall time 或理想全双工上限。

如果 pinned 页 `move_pages` 返回 EFAULT，或容器无法查询策略，保留 pinned 验证与 NUMA 归属为两个字段。Affinity 设置成功不证明页位置；整段 VMA 的 NUMA 分布也不等同精确 buffer 分布。

最终代码结论附版本、文件/行号/哈希、实际调用协议、匹配实验和未覆盖的 Runtime 内部层级。源码候选保持 candidate，实际调用/trace 是 observed，跨层推理标 inferred；没有证据的部分留 unknown。整体决策流程回到 [diagnostic-playbook.md](diagnostic-playbook.md)。
