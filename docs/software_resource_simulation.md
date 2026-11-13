# DSP / FPGA 软件资源仿真与规划职责

本项目的 DSP / FPGA 采用资源和成本模型，无需物理设备、Docker Engine 或 HDL 工具。
虚拟节点负责容量、背景占用、资源预留、链路和执行时间估算；信号数据仍通过已有 Python CPU 组件计算。
这验证调度策略和组件功能，不是 DSP 指令集仿真、FPGA RTL 仿真或硬件加速测试。

当前正式配置已升级为 [66AK2H06 四核 / XC7K410T 四区域参考模型](reference_hardware_model.md)。DSP 按核心计算与本地存储分配，FPGA 按每区域 LUT、FF、DSP48、BRAM36 分配。旧抽象槽仅保留格式兼容。

## 调研与选择

| 路线 | 能解决的问题 | 本次选择 |
| --- | --- | --- |
| 离散事件资源仿真，例如 [SimPy](https://simpy.readthedocs.io/en/stable/) 的 [Resource / Container](https://simpy.readthedocs.io/en/stable/api_reference/simpy.resources.html) | 请求、占用、释放有限容量，适合任务到达与资源竞争 | 采用相同的容量/生命周期思想，复用现有 TaskScheduler，不增加第二套调度引擎或 SimPy 依赖 |
| 数值信号处理，例如 [SciPy signal](https://docs.scipy.org/doc/scipy/reference/signal.html) | 滤波、频谱分析等算法的功能参考 | 复用项目已有 NumPy 组件，无需为了资源仿真重写 FFT、DDC、滤波等功能 |
| [cocotb](https://docs.cocotb.org/en/stable/) 与 [HDL 模拟器](https://docs.cocotb.org/en/stable/simulator_support.html) | 验证 Verilog / VHDL 设计的逻辑行为 | 需要 HDL 设计与对应模拟器；当前任务是资源调度，暂不引入 |

SimPy 能描述资源竞争，不能自动给出某个 DSP / FPGA 的真实处理能力；cocotb 也不会自动把现有 Python 组件变为硬件实现。

## 运行

在项目根目录：

```powershell
.\.venv\Scripts\python.exe -m examples.heterogeneous_simulation_demo
```

示例加载真实频谱分析业务请求并补充明确截止时间，通过正常细化与静态匹配得到 C03 → C05 → C27。
使用一个主机 CPU、四个 C66x 核心和四个 FPGA 区域，共九个可放置节点与完整有向链路，处理四帧 IQ 数据。
结果保存为 `outputs/heterogeneous-simulation.json`。
示例会分配虚拟 DSP 与 FPGA，执行信号处理后清理实例并恢复任务提交前的背景占用。

使用同一 API 和前端：

```powershell
$env:MRF_DEPLOYMENT_CONFIG = 'examples/ResourceSimulations/heterogeneous_simulation.json'
.\.venv\Scripts\python.exe -m uvicorn backend.api.app:app --host 127.0.0.1 --port 8000
```

前端按原有方式启动。资源页显示软件仿真标识、逐核与逐区域的容量及占用，不显示虚构的硬件实际使用趋势。
现有任务提交接口使用同一规划器。画像覆盖频谱链 C03→C05→C27，以及工作台数据链接收样例 C15→C14→C17→C35→C36 的精确参数。接收链提供 CPU 参考画像，配额、内存和耗时均为明确标注的演示假设，不是硬件测量；其他业务仍需补充画像与边契约。

换任务可在任务页点击“载入接收链样例”，载入配套的两帧合成 IQ 后提交执行，最终恢复演示消息 `0123456789abcdeffinal`。固定硬件卡片不会随任务切换消失；资源页分别列出背景、执行和 Warm 常驻预算。需要空池测试时可点击“释放空闲 Warm”，此操作不清除配置中的背景占用。取消 Warm 保留只影响后续提交，不会自动清除先前任务的 Warm 实例。
任务必须提供明确完成截止时间。原始频谱请求未给截止时间时，预览会返回 `UNVERIFIED_TIME`，不会直接执行。

## 配置与单位

- `examples/ResourceSimulations/heterogeneous_simulation.json`：节点容量、节点特性、有向链路、边传输契约、完整背景占用。
- `examples/ResourceSimulations/heterogeneous_simulation_profiles.json`：组件在各虚拟节点上的合成执行画像。
- `cpu_millicores`、`memory_bytes`、`bandwidth_bytes_per_s` 继续使用明确单位；此处都是模型参数，不能据此声称真实机器已具备相应容量。
- 正式 schema 2 用核心计算预算与本地 L2 描述 DSP，用 LUT、FF、DSP48、BRAM36 描述 FPGA 区域；旧版 schema 1 中 dsp_units、fpga_units 的抽象槽含义仅用于兼容。
- `runtime_p95_ms`、`setup_p95_ms` 沿用已有字段以兼容规划器；当 `timing_scope=synthetic` 时是人为设定的时间成本，不是采样得到的统计 P95。`sample_count=1` 是兼容字段，`source` 明确记录为假设模型。
- 必须显式填写每个节点与每条有向链路的初始占用，避免遗漏被误当成空闲。背景占用可以非零。
- 合成画像须精确匹配 CID、组件参数、hardware_id 和所需特性。软件执行还要求 CID 已有 Python 参考实现，并检查输入输出字节数未超过画像包络。

虚拟 DSP / FPGA 不通过修改 Docker 的 `processor` 检查来伪装执行。
`SoftwareSimulationRuntime` 是独立的软件适配器；正常 Docker 路径继续仅支持本机 CPU。

## 调用与状态

```text
配置 → load_resource_simulation → PlacementPlanner + PlacementState
                              ↓
DeploymentManager → TaskScheduler → 现有 exact / chain_dp / heft
        ↓                    ↓
SoftwareSimulationRuntime   节点与链路预留
        ↓                    ↓
Python 组件处理数据 → 清理实例 → 释放本任务预留，保留背景占用
```

`PlacementPlan.profile_basis=synthetic` 与 `execution_mode=software_simulation` 在输出和 API 中保留来源。
快照中的实例状态为 `SIMULATED`、执行后端为 `python_cpu_reference`，不会上报硬件 CPU / 内存统计。
实际 Python 耗时不等于模型预测耗时；不使用 sleep 伪装加速，链路也由代价模型估算而非真实网络传输。

本次复用现有事件入口：提交增加资源占用、完成或失败释放占用，多个活动任务共享同一调度器状态。
未加入随机任务到达、周期性监测或随时间自动变化的背景流量。若需要负载曲线与排队策略，后续可在这些入口外增加事件驱动器。
任务内多帧复用组件实例；显式设置 `keep_warm=true` 可跨任务复用 Warm 仿真实例。常驻的 DSP 本地存储和 FPGA 逻辑预算继续计入占用，详见 [模块四开发与验收](module4_implementation.md)。

## 规划逻辑是否重复

| 文件 / 入口 | 保留职责 | 资源聚合语义 |
| --- | --- | --- |
| `resources/models.py` | 资源等级、任务资源字段和归一化画像的数据结构 | 不执行分配 |
| `resources/measurements.py` | 带明确单位和来源的实测 / 合成画像结构、加载与兼容性筛选 | 不执行分配，不要求数据必须来自真实设备；合成数据须显式标记 |
| `ResourcePlanner.plan()` | 旧版归一化 CPU 串行估算，保留兼容 | CPU、带宽取单节点峰值，常驻内存求和；不选择 DSP / FPGA |
| `ResourcePlanner.plan_physical()` | 单设备物理单位串行诊断，保留兼容 | 处理器与带宽取峰值，内存、本地存储及 FPGA 资源求和；没有多核/区域放置、链路与帧数优化 |
| `PlacementPlanner.plan()` | 新异构资源分配的统一入口，供软件仿真与真实 Docker 路径复用 | 每节点所有实例的全部资源维度累加，并预留跨节点链路，支持帧数和多种算法 |

两条规划路径都检查容量和时间，概念上有重叠，但语义不同；直接合并会改变已有任务的结果。
例如两个各需 1 核的组件：串行峰值估算为 1 核；容器配额预留为 2 核。二者不能混用同一份已占用状态。

本次没有另建 DSP / FPGA 专用规划器，而是复用 `PlacementPlanner` 和现有三种优化算法。
共用的截止时间逻辑已移至 `resources/constraints.py`，避免多节点规划依赖旧归一化规划器。
新功能统一接到 `PlacementPlanner`；旧入口只维护兼容与诊断，不继续扩展一套并行的异构调度逻辑。
单设备兼容入口使用合成画像时返回 `FEASIBLE_SIMULATION_ESTIMATE`，避免误标为实测可行。

## 验证范围

新增测试验证 DSP / FPGA 容量耗尽、竞争任务拒绝、CPU 回退、三种算法可运行、失败清理、背景占用恢复、
真实参考信号输出一致、输入字节包络以及 API 来源标识。
模型结果只能说明给定假设下的调度行为。要讨论不同硬件的性能，需要明确参数来源并做敏感性分析。
