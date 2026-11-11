# 资源放置对比实验

这里的场景及资源画像均为合成仿真数据，不能作为 Docker 或异构硬件的实测性能。
`scenarios.json` 固定图规模、形状、设备数、随机种子和帧数。
`baseline.json` 保存当前运行环境、场景、资源占用和完整结果；`baseline.csv` 便于绘图和比较。

在仓库根目录运行：

```powershell
.\.venv\Scripts\python.exe -m experiments.resource_allocation.benchmark
```

可以用 `--scenarios`、`--output` 和 `--max-assignments` 指定场景、输出文件及候选扩展上限。
规划耗时是本次机器上的观测，重复运行会变化。只有 Exact 已证明最优时才计算相对最优代价比；触及搜索上限的结果不能作为最优基准。

所有算法优化相同的串行估算：

```
sum(setup) + frame_count * (sum(process) + sum(cross_host_transfer))
cross_host_transfer = latency_ms + bytes_per_call * 1000 / reserved_bytes_per_s
```

同机 HTTP、序列化及反序列化开销可由 `docker_http_roundtrip` 画像覆盖；图执行校验和其他调度开销仍可能形成偏差。组件 P95 相加不是任务实测 P95。

首轮完整搜索的小场景中，链式 DP 和 Exact 的代价一致。HEFT 在 chain-8 和 branch-8 找到相同代价，但 chain-5 的代价约为最优解的 2.95 倍。20 节点链的 Exact 和 DP 均触及 30000 次扩展上限，DP 未到达完整分配阶段；HEFT 找到可行解。这些结果展示当前策略的边界，不能证明任意 DAG 的近似比或 DP 的普遍加速。

DP 仅支持单一线性链，以最后节点的处理设备与输出字节包络区分边界，同时保存所有设备的五维占用及全部链路预留。代价和各项资源均不更大时才进行支配剪枝。剪枝按边界分组，并批量比较资源向量。内存和运行时间仍可能随非支配标签数增长；扩展上限始终有效。

HEFT 使用平均节点代价与通信代价计算 upward rank；按优先级选择就绪节点，按串行增量代价选择处理设备。容量冲突时进行有界回溯。该策略未使用并行最早完成时间估算当前 Docker 执行器。

调用现有链路：

```python
plan = planner.plan(task, match, state, algorithm="chain_dp", frame_count=32)
preview = scheduler.preview_placement(task, match, algorithm="heft", frame_count=32)
result = manager.execute(task, match, frames, algorithm="heft", frame_count=32)
```

默认仍为 `exact`。`PlacementPlan` 保留原字段，并增加 `algorithm`、`optimality_proven`、`frame_count` 和 `estimation_basis`。最优性只针对给定静态候选、兼容测量画像及当前代价模型。

- `FEASIBLE_ESTIMATE`：资源满足且估算时间满足截止时间；启发式结果也可使用此状态。
- `SEARCH_LIMIT`：候选扩展或静态候选枚举被截断，可能含完整分配，但调度器不提交该状态。
- `UNSUPPORTED_GRAPH`：链式 DP 收到非线性图。
- `HEURISTIC_FAILED`：启发式方案未满足截止时间，不代表已证明全局不可行。
- `INFEASIBLE`：搜索已证明无资源可行分配，或已证明的最优估算也超过截止时间。
- `NEEDS_CALIBRATION` / `NEEDS_TRANSFER_PROFILE` / `UNVERIFIED_TIME`：继续沿用缺少画像、传输数据或截止时间的诊断。

部署对有长度的输入自动获取帧数；未声明帧数的生成器会先收集为有限批次。给生成器显式传入 `frame_count` 可以保留流式消费，帧数不符时执行失败并走原有清理流程。

## Docker 标定

默认接收链仍可使用：

```powershell
.\.venv\Scripts\python.exe -m deployment.calibrate_local --frames 32 --output-dir outputs/calibration-rx
```

其他可执行组件图需提供匹配结果中的 `execution_graph` JSON 和输入帧文件：

```powershell
.\.venv\Scripts\python.exe -m deployment.calibrate_local --execution-graph graph.json --frames-json frames.json --frames 20 --output-dir outputs/calibration-custom --image mrf-component:local
```

`frames.json` 为数组，每帧按根节点 ID 映射到 `deployment.protocol.encode_packet()` 的结果；多端口根节点则映射到 `{port: encoded_packet}`。必须提供与 `--frames` 相同数量的输入。

输出保留 v1 部署配置与画像格式，新增可选 `timing_scope` 和 `setup_sample_count`，旧画像仍可加载。报告保存镜像、Engine、执行图、输入摘要、原始节点和帧耗时、整帧 P95、单次会话观测以及模型估算。容器启动当前每节点测一次，不能称为实测启动 P95；单次会话观测也不是任务会话 P95。

标定只支持现有本机 CPU Docker runtime。DSP/FPGA 画像需要在相应硬件与实现上另行实测；实验中的加速器画像没有部署能力含义。
