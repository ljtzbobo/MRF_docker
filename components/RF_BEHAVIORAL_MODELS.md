# 射频前端行为模型

当前交付重点为：基础 ADC/DAC、频率和带宽约束、LNA 噪声、本振调谐开销。
多射频通道暂缓，没有登记多通道组件或多通道协议，也不声称完成多天线同步收发。

## 调研与建模方法

- [MathWorks RF Noise Modeling](https://www.mathworks.com/help/comm/ug/rf-noise-modeling.html)：以增益、噪声系数描述放大器的附加噪声。
- [MathWorks Memoryless Nonlinearity](https://www.mathworks.com/help/comm/ref/comm.memorylessnonlinearity-system-object.html)：复数基带上的 PA 非线性模型。本项目独立实现 Rapp AM/AM 与可选简化 AM/PM，不依赖 MATLAB。
- [GNU Radio Channel Model](https://www.gnuradio.org/doc/sphinx-3.7.8/channels_blocks.html)：样本域的噪声、频偏和采样误差建模思想；不复制 GNU Radio 的实现代码。
- [ADI ADC 时钟优化](https://www.analog.com/en/resources/analog-dialogue/articles/analog-to-digital-converter-clock-optimization.html)：时钟抖动的影响与输入模拟频率有关，不能只看基带包络频率。

采用离散时间、复数等效基带模型，而非电磁仿真或 SPICE 电路求解。
所有输出都是采样数组；不打开设备、不发射电磁波、不把行为模型替换为任务中泛指的真实射频硬件。

## 单位与接口

复数样本按 RMS 包络电压约定，平均功率为 `mean(abs(x)**2) / R`；输入幅度必须由调用方正确标定。
这是新增 RF 行为模型的约定，原有通信组件的归一化符号幅度不自动代表伏特。
端口为一维复数 IQ，使用已有 `iq/complex`、`iq/baseband`、`time/complex` 协议。
`process()` 为独立数据块，状态在块间重置；`push()` 为连续流，保存滤波历史、混频相位、噪声 RNG、ADC 抽样相位。
当前 Docker worker 使用 process，所以跨请求连续振荡器状态还需单独适配流接口，不能据此声称已完成连续硬件流。

## 已登记模型

| CID | 模型 | 实现范围与边界 |
| --- | --- | --- |
| C38 | 天线/孔径方向增益 | 高斯近似主瓣，半功率全宽；未计效率的方向增益乘效率平方根，不解电磁场、极化或传播损耗 |
| C39 | 射频开关 | 插损、关断隔离、初始稳定样本数；改变开关状态需重置流后配置 |
| C40 | 双工器隔离 | left 为期望接收信号，right 为同参考频率/采样时刻的发射泄漏，按插损和隔离叠加 |
| C41 | 射频带通近似 | 有状态复数窗化 FIR；offset 为相对于 IQ 参考中心的频偏，双边通带须位于采样 Nyquist 范围内 |
| C42 | LNA | 固定增益与噪声系数模型；没有 IP3、压缩或频率相关噪声系数 |
| C43 | VGA | 增益上下界、离散增益步长；动态 AGC 继续使用已有 C04 |
| C44 | 模拟 IQ 混频器等效模型 | 上下变频、转换增益、I/Q 增益/相位失配和 LO 泄漏；不是晶体管级混频器 |
| C45 | LO / PLL 波形行为 | 频偏、指数衰减的初始频率误差、维纳相位噪声；不是完整 PLL 环路或指定相位噪声频谱 |
| C46 | 基础 ADC | I/Q 独立有符号均匀量化、饱和、抽取与采样率/时间戳更新 |
| C47 | 基础 DAC | 有限位数量化、插值后的零阶保持采样网格；不生成连续模拟波形 |
| C48 | PA | Rapp 软压缩，可选经验 AM/PM，未建热效应或记忆效应 |
| C49 | 参考时钟 | 小抖动一阶近似与参考相位误差，包含指定载频的相位敏感性；未建采样率漂移闭环 |

所有能力 ID 均以 `_sim` 显式标记。C33/C34 保留为原有粗粒度前端近似，不自动扩展成上述物理链。
新增组件尚无 DSP/FPGA 综合或 CPU 标定画像；不能自动将它们列为已验证的硬件加速实现。

## 基础 ADC / DAC

位宽 2–24，满量程 ±full_scale_v，量化步长 `2*full_scale_v / 2**bits`。
采用 round-to-nearest 的有符号重构电压，范围 `[-full_scale_v, full_scale_v-LSB]`；输出仍为复数电压，不是整数码流。
元数据和公共运行指标记录位宽、LSB 与 I/Q 任一分量过载的复数样本数。
ADC 支持整数抽取，DAC 支持整数重复保持，倍率 1–64。
静态边匹配和 DAG 执行均检查变换后的采样率与块长；连续 ADC 流会保持抽样相位。
ADC 不替调用方自动进行抗混叠：抽取前应串联 C41/C05；DAC 后可串联 C41 作为重建滤波近似。
未实现 ENOB/DNL/INL、逐比特编码、真实采样保持电路或连续时间转换。

## LNA 噪声

`F = 10**(noise_figure_db/10)`，新增输入参考复噪声方差为：

```text
variance_v2 = k * T * B * R * (F - 1)
y = 10**(gain_db/20) * (x + complex_noise)
```

噪声两正交分量各占一半方差，固定 seed 可复现，连续 push 分块不改变样本序列。
该模型只添加放大器的额外噪声；输入环境噪声由输入信号提供。noise_bandwidth_hz 指定积分噪声带宽，
当前生成的是采样网格上的白噪声，不单独生成对应频谱形状；带宽不得超过复数采样率。
temperature_k 是此模型假设的参考温度，不据此推断真实器件的噪声温度曲线。
测试验证大样本噪声功率与模型方差符合统计误差范围。

## 频率、带宽与本振调谐

`rf/tuning.py` 提供 RFOperatingRange 和 LOTuningController。
默认频率 30MHz–6GHz、带宽 1kHz–40MHz、最大采样率 61.44MS/s 均为**示例假设，不是某个指定型号的完整规格**。
检查占用频段的上下边缘，不仅检查中心频率；复数采样率必须覆盖完整带宽，并满足转换器采样率上限。
混频与 RF 滤波组件还会检查包络频偏和通带是否越过 Nyquist。

调谐开销：初次上电为 base_lock_ms；后续配置变化为：

```text
base_lock_ms + abs(new_frequency-old_frequency)/slew_hz_per_ms
             + bandwidth_switch_ms（带宽或采样率变化时）
```

已锁定且设置未变则开销为零。preview 不改变状态，tune 提交目标与 ready_at_ms；
锁定前的重调谐或 require_ready 会拒绝。时间为虚拟模型时间，不使用 sleep 假装硬件锁定。
本振控制器需由调用方维护为对应射频前端的长生命周期对象，尚未自动绑定业务节点或做多通道共享。

LOTunePlan.apply_to_profile(profile) 生成带 `tuning_setup_ms` 的组件执行画像。
规划器的 `effective_setup_ms = setup_p95_ms + tuning_setup_ms`；Exact、Chain DP、HEFT 及单设备兼容入口均计入此开销。
不改变基础冷启动画像，也不会把相同调谐开销计入所有组件；调用方应仅应用到负责调谐的节点画像。
默认旧画像 tuning_setup_ms=0，保持原有规划行为。测试验证调谐超过截止时间会被三种算法拒绝。
当前没有自动从任意任务请求生成前端标定画像，未提供画像时仍应返回缺标定诊断。

## 运行与其他原型

```powershell
.\.venv\Scripts\python.exe -m examples.rf_frontend_behavior_demo
```

结果保存在 `outputs/rf-frontend-behavior.json`，包含逐级功率、量化过载、采样率、LO 准备时间与孔径释放状态。
演示为离线发射链→接收链回环，不是空中传播或同时收发。
SharedAperture 是显式调用的逻辑 TDD 租约：允许多个接收者共享，发送期间互斥；尚未接入 TaskScheduler 或多通道业务路由。
天线、开关、双工器、VGA、混频、PA 和时钟组件作为行为原型一并登记，不能将其等同于对应硬件已完整实现。
