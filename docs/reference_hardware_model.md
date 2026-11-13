# 正式资源参考模型：66AK2H06 / XC7K410T

模型标识为 `66AK2H06-XC7K410T-v1`，配置与执行画像使用 schema version 2。
该标识固定了芯片参考、DSP 四核映射、FPGA 四区域预算和资源单位。后续改变区域政策应更新模型标识和校验器。

## 官方规格与模型假设

| 参数 | 采用值 | 来源与性质 |
| --- | --- | --- |
| DSP 型号 | TI 66AK2H06 | [TI 产品页](https://www.ti.com/product/66AK2H06) 标题明确为四个 C66x 核心、两个 ARM A15；不要将系列通用八核描述套到此 SKU |
| C66x 核心 | 4 个 | [TI 系列数据手册](https://www.ti.com/lit/ds/symlink/66ak2h06.pdf)，型号差异表及 66AK2H06 功能图 |
| 每核频率参考 | 1.2 GHz，即 1200 百万核心周期/秒 | 数据手册支持的工作频率；配置可降低预算，不可超过此模型上限 |
| 每核本地 L2 | 1024 KiB | 数据手册的每 CorePac 本地 L2；本模型将其作为软件可分配上限 |
| FPGA 型号 | Xilinx / AMD XC7K410T | [Kintex-7 产品页](https://www.amd.com/en/products/adaptive-socs-and-fpgas/fpga/kintex-7.html) |
| FPGA LUT / FF | 254200 / 508400 | [AMD UG474 CLB 资源表](https://docs.amd.com/r/en-US/ug474_7Series_CLB/7-Series-FPGA-CLB-Resources) |
| FPGA DSP48E1 / BRAM36 | 1540 / 795 | [AMD UG586 器件资源表](https://docs.amd.com/api/khub/documents/rTaW6qs7YptHTdMLuG0kVA/content) 中 XC7K410T；BRAM 为 36Kb 块 |
| FPGA 四区域与静态预留 | 20% 静态，80% 分为四个相同逻辑区域 | **本项目的仿真假设**，不是厂商预制分区或已完成布局布线的 Pblock |

CPU 节点 `sim-cpu` 是独立的通用主机参考节点，不代表 66AK2H06 内的两颗 ARM 核心。
DSP 的 L1、共享 MSMC、DDR 控制器等未进一步建模；FPGA 的时钟、布线、I/O、跨时钟域和真实 DFX 位流不在此模型内。

## 节点与身份

```text
独立主机 CPU
66AK2H06-0
  ├─ c66x-core-0：独立计算预算、独立本地 L2
  ├─ c66x-core-1：独立计算预算、独立本地 L2
  ├─ c66x-core-2：独立计算预算、独立本地 L2
  └─ c66x-core-3：独立计算预算、独立本地 L2
XC7K410T-0
  ├─ xc7k410t-region-0：独立 LUT / FF / DSP48 / BRAM36 预算
  ├─ xc7k410t-region-1：独立 LUT / FF / DSP48 / BRAM36 预算
  ├─ xc7k410t-region-2：独立 LUT / FF / DSP48 / BRAM36 预算
  └─ xc7k410t-region-3：独立 LUT / FF / DSP48 / BRAM36 预算
```

这九个可放置节点属于三个设备：主机、DSP 芯片、FPGA 芯片。
每个核心或区域使用唯一 hardware_id；device_id 表示所属芯片，processor 和 unit_index 表示类型与索引。
分配输出携带 device_id、core_id 或 region_id，资源状态逐核心、逐区域保存。

## DSP 两维预算

- `dsp_compute_mcycles_per_s`：百万核心周期/秒，每核上限 1200。
- `dsp_local_memory_bytes`：本地 L2 字节数，每核上限 1048576。

每个组件的候选画像声明其计算预留与本地存储需求。允许多个组件共享同一核心，但二者的**需求之和**均不得超过该核的剩余预算。
不能将四核内存相加后允许一个组件占用超过单核 L2，也不能将四核总计算预算当成一个可任意拆分的处理器。
单个组件实例当前只放在一个核心，不支持自动跨核拆分。

“核心周期”不是 MAC / FLOP；不同指令、缓存命中与流水线行为会改变实际吞吐。
组件的周期需求、负载率和运行时间目前分别作为模型输入，尚未由 C66x 编译或性能分析推导。
频率上限有硬件参考，并不意味着示例 runtime_p95_ms 已得到该芯片验证。

## FPGA 四维预算

| 资源 | 整片 XC7K410T | 静态预留 20% | 每个逻辑区域 | 四区域总预算 |
| --- | ---: | ---: | ---: | ---: |
| LUT | 254200 | 50840 | 50840 | 203360 |
| FF | 508400 | 101680 | 101680 | 406720 |
| DSP48E1 | 1540 | 308 | 308 | 1232 |
| BRAM36 | 795 | 159 | 159 | 636 |

对应字段为 `fpga_lut`、`fpga_ff`、`fpga_dsp48`、`fpga_bram36`。
资源计数必须为非负整数；BRAM36 是 36Kb 块数量，不是字节，也不是 18Kb 块数量。
一个组件必须完整放入同一个区域的四维剩余预算。多个组件可在区域中共存，但四项需求逐项累加。
即使其他区域还有空余，也不会借用它们的 LUT 或 BRAM 来弥补当前区域不足。
四区域预算总和低于整片上限；这是资源数量上的约束，不能据此证明区域在真实芯片上可布线或满足 DFX 边界要求。

## 组件实现候选

示例仍使用真实业务 C03 → C05 → C27；每个组件具有 CPU、四个 DSP 核心、四个 FPGA 区域的候选画像。
不同处理器具有不同需求与时间成本，规划器可依据负载与资源竞争选择实现方案。

| 组件 | DSP 计算预留 MCycles/s | DSP 本地 L2 KiB | FPGA LUT | FF | DSP48E1 | BRAM36 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| C03 下变频 | 250 | 64 | 9000 | 15000 | 96 | 24 |
| C05 滤波 | 450 | 128 | 16000 | 24000 | 240 | 48 |
| C27 频谱感知 | 700 | 256 | 36000 | 54000 | 200 | 112 |

**此表是用于制造资源竞争的合成工作负载，不是综合、编译或设备测量结果。**
例如 C05 与 C27 放在同一区域时，LUT、DSP48 和 BRAM 都会超过区域预算，必须分开或选择 DSP 实现。
核心频率、区域容量有官方规格参考；组件需求、启动成本、处理时间、网络带宽和时延仍属仿真假设。

## 三种算法如何使用约束

三种算法共用 `PlacementProblem.evaluate()`：逐放置节点累加所有资源维度、叠加已有占用，然后逐项检查容量和有向链路。
Exact 枚举可行组合，Chain DP 的支配标签包含全部核心/区域资源维度，HEFT 的选择和回溯同样调用该检查器。
完整方案还会再做一次独立完整约束检查。Chain DP 仍限于线性链；HEFT 不保证最优。
这一步增加了真实资源种类的竞争约束，目标时间仍为现有串行模型，不等于并行硬件调度或真实加速器耗时。

## 配置与兼容

- 正式配置：`examples/ResourceSimulations/heterogeneous_simulation.json`。
- 正式画像：`examples/ResourceSimulations/heterogeneous_simulation_profiles.json`。
- schema 2 的容量、占用和需求必须完整填写十一项通用资源字段，其中旧 `dsp_units` / `fpga_units` 均为零。
- schema 1 的旧五维 JSON 仍可加载，用于原实验、旧标定与兼容测试；它们不代表本正式参考模型。
- `resources/models.py` 中旧任务的 dsp_units/fpga_units 输入字段也保留兼容，不会自动换算为 LUT 或核心周期。
- 当前默认背景占用：core-0 使用 100 MCycles/s 与 64KiB L2；region-0 使用 8000 LUT、16000 FF、40 DSP48、20 BRAM36。
- 普通完成释放全部任务预留。调度器显式 keep_warm 时保留 L2 与 FPGA 常驻资源，逐出预热记录后再释放；这仍不是跨任务容器复用。

运行三种算法：

```powershell
.\.venv\Scripts\python.exe -m examples.heterogeneous_simulation_demo --algorithm exact
.\.venv\Scripts\python.exe -m examples.heterogeneous_simulation_demo --algorithm chain_dp
.\.venv\Scripts\python.exe -m examples.heterogeneous_simulation_demo --algorithm heft
```

示例在 CPU 上执行实际信号处理，同时输出上述参考资源模型的分配与成本；输出仍明确标记 software_simulation / synthetic。
测试覆盖每个 FPGA 维度耗尽、DSP 每核计算/本地存储耗尽、区域不可拼接、Exact 与 DP 的独立枚举最优值、
竞争任务拒绝、资源释放和预热保留、旧画像兼容，以及配置超过参考上限时的拒绝。
