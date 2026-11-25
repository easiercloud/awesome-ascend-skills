# 0.1.0 验证记录

验证日期：2026-11-25。生产脚本使用 Python 3.9+ 标准库；硬件测试使用机器原有的 CANN、torch/torch_npu、npu-smi、lspci 和 perf，未安装驱动或修改 BIOS。

## 已完成的真实运行

| 范围 | 覆盖 | 验收 |
| --- | --- | --- |
| AMD / Ascend950PR，8 个可见逻辑设备，选中 0、1 | ACL：pinned/pageable × H2D/D2H/HD × 两张卡逐卡 solo/两卡 concurrent，共 18 case | 18/18 完整样本、内容验证、CPU governor performance |
| 同一主机，已有 CANN 容器，选中 0、1 | torch：相同 18 case | 18/18 完整样本、内容验证、容器 host PID 归属与结束守卫 |
| 同一主机，选中 NPU2 | ACL pinned D2H，256 MiB ×32，3 repeats；AMD DF IOM 计数 | IOM3/Socket0 主流量，32 指标；主事件 running 100%、完整覆盖；observer 字节/定时 DMA 字节 = 1.02434 |
| AMD/950PR 与 Intel/950DT，两台各选 0、1 | ACL pinned D2H，256 MiB ×64，2 repeats，CPU performance；服务器侧协调器 | 4 张卡均完成并重验；按记录的校时误差界，全卡 host loop 共同窗口下界分别为 204.71/204.44 ms |

前两行采用 64 MiB ×8、1 warmup、3 repeats 的 quick profile，验证执行路径和测量口径；没有据此宣称应用峰值或测试全部设备。标准矩阵默认 256 MiB ×32、3 warmup、5 repeats。

多机运行的严格起跑对齐门槛保持原值；该轮约 292 ms 的校时起跑误差界未通过 200 ms 门槛，因此 `synchronization_status=unsynchronized`。独立的 `overlap_status` 根据每台主机全卡样本交集和各自 clock error，证明两轮 **host 计时循环窗口**重叠；这些窗口包含提交和等待，不证明 DMA engine 始终同时忙碌，也不产生一个跨机总带宽。

另一次真实双机尝试在准备后遇到 16 卡训练进入，自动取消自有测试，并确认双方 cleanup。该轮没有进入成功吞吐覆盖。

原始运行含实际连接、硬件序列号与完整系统输出，保留于测试者私有归档。公开仓库包含下述脱敏历史重放；不把以上未公开记录冒充成可从公开样例复算的证据。

## 独立离线试用与回归

公开的 `examples/pcie-gen5-pairs.json` 是真实历史实测的脱敏重放：13 case、115 sample、23 每卡表行，以及完整 841 PCI functions、92 条真实父子边、18 NIC、6 NVMe。公开结构事实、计时和字节量可复算；原始系统 stdout 和私有身份信息未公开。实际基准覆盖逻辑设备 0、1、2、4 和 pinned，其余设备/内存维度在报告中保留 unknown。

独立试用从 SKILL 与公开原始数据运行 replay、report、16 卡全矩阵 plan，复核样本、共同时间窗、CSV 和图的节点/边完整性。随后补齐了双向反证链、单卡 HD 疑点、负实验、版本匹配、pageable 降速与真正异常方向的实验选择。

自动检查覆盖：计时/字节与内容核验、ACL ABI、pinned/pageable HD 协议、可见设备重排、非连续 ID、多级 PCI 树、稀疏 socket、静态 IIO、型号门控 PMU、缺测/单位/缩放、原始哈希、恢复签名、进程退出竞态、容器传输、CPU 策略恢复、真实训练冲突处置、fleet barrier/取消/校时/窗口重叠、HTML/CSV 注入及图结构保真。

```bash
python3 scripts/validate_skills.py
python3 -m pytest -q skills/base/ascend-dma-diagnose/tests
```

浏览器验收包括默认整机 I/O 图、点击 NPU 到根总线细页、搜索折叠 BDF 到全 841 节点树、折叠链路观测、计时分解和 PMU 原始证据链接。HTML 资源离线内嵌。

## 范围

DMA backend 的可用性由已有 Runtime/API、设备映射和实际验收决定。PMU 自动适配限于文档中已验证的 CPU 型号/format；其他 CPU 只记录能力，不复用 raw 编码。BIOS/BMC、精确 pinned 页落点、物理 switch ASIC 边界和 CPU 内部某一级硬件上限，仍需要各自的证据。
