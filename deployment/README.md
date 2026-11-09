# 数据链接收的最小容器部署

部署入口是 `TaskService.execute_deployed(请求, 帧序列)`，HTTP 入口是 `POST /api/v1/deployments/execute`。服务按当前业务规则细化请求、静态匹配完整规格 DAG，调用物理放置规划器预览并提交，然后按组件执行图的拓扑顺序启动容器、传输每帧数据，结束或失败时停止容器并释放调度预留。`POST /api/v1/plans` 在启用部署配置后返回同一物理放置器的预览。旧版任务仿真入口仍可独立使用。

## 部署配置

`config.example.json` 声明本机处理节点、硬件容量文件、实测组件画像文件、四条边的传输画像和 C15/C14/C17/C35/C36 的镜像。五个 CID 当前使用同一镜像，每个容器以 `--cid` 和节点 JSON 参数启动一个组件实例。镜像应由本项目构建：

```powershell
docker build -f deployment/Dockerfile -t mrf-component:local .
```

示例硬件文件只是配置格式示范。项目内 `resources/data/measurements.json` 没有实测记录；部署前必须将配置的 `measurements` 指向含有当前 CID、参数、硬件 ID、输入输出字节数、执行时间和资源需求的测量文件。缺少画像时返回 `NEEDS_CALIBRATION`，不会创建容器。请求也必须给出明确截止时间；否则返回 `UNVERIFIED_TIME`。

本机 Docker Engine 可用时，先构建镜像，再标定当前接收链：

```powershell
python -m deployment.calibrate_local
```

脚本运行 32 帧 `aligned_iq_frame()` 演示输入，按 C15→C14→C17→C35→C36 实际启动容器并测量 HTTP 调用时间、输入输出字节数、CPU 使用计数与容器内存峰值。输出 `deployment/hardware.local.json`、`measurements.local.json`、`config.local.json` 和原始报告 `calibration.local.json`。硬件容量取自当前 Docker Engine，画像与镜像 ID、CID 参数和 Engine ID 绑定；更换镜像、参数或 Docker 主机后应重测。CPU 每组件预留 1 核，与标定时的限额一致；内存预留为观测峰值的 1.5 倍且至少 64 MiB。启动仅采样一次，`setup_p95_ms` 是该次观测值，不能视为统计意义上的 p95。所有组件同机，经回环 HTTP 传包；外部链路带宽未测量，容量与需求均记为 0。此画像用于本机演示接收链，不代表真实无线链路或其他业务。

规划器目前按一次 DAG 调用的启动与处理时间判断 `FEASIBLE_ESTIMATE`；多帧任务的总时长还需结合帧数重新估算。前端展示的可行状态是提交门禁，执行时后端会重新预览和预留。

运行演示：

```powershell
python -m examples.data_link_docker_demo --config deployment/config.local.json
```

仅验证部署流程时，可先构建镜像并设置 `MRF_TEST_DOCKER=1` 运行 `python -m unittest tests.test_deployment`。该测试在内存中构造标记为测试用途的画像，实际启动五个容器并传送 32 帧、重组 500 字节报文，最后检查资源释放；测试画像不应复制到正式测量目录。未设置该变量时真实 Docker 测试自动跳过。

运行 `deployment/start_local_api.ps1` 会将 `MRF_DEPLOYMENT_CONFIG` 设置为本机配置并启动 API。也可手工设置该环境变量后启动 `uvicorn backend.api.app:app`。未配置时部署接口返回 503。API 请求体为 `{"task": 外部业务请求, "frames": [{"入口节点ID": 数据包}, ...]}`；数据包使用 `deployment.protocol.encode_packet()` 的 JSON 结构。`python -m examples.generate_data_link_upload` 可重新生成工作台内置的两帧演示请求与输入帧。

## 容器接口

每个容器在内部 8765/TCP 提供 `GET /health` 和 `POST /process`。传输只发布到当前 Docker 主机的 `127.0.0.1` 随机端口。`/process` 请求为 `{"inputs": {"in": 数据包}}`，响应为 `{"output": 数据包}`；双输入组件使用 `left`、`right` 等命名端口。数据包包含 `dtype`、`shape`、`data_base64`、`sample_rate`、`center_frequency`、`timestamp`、`domain`、`metadata`，数组上限为 16 MiB。服务按执行图的端口边将前一容器输出转发到后一容器；跨帧会话复用同一容器实例以保留重组状态。

当前部署器只支持一个本机 Docker 处理节点和 CPU 组件画像。跨主机传输、持久化恢复、硬件 DSP/FPGA 执行以及真实无线同步和信道均衡不在此闭环内。放置器的时限判断取决于提供的测量画像；HTTP、容器启动开销和多帧处理总耗时也需要在实际部署画像中测量。若容器启动后的清理或最终停止失败，调度预留会保留并报错，避免可能仍在运行的容器对应资源被重复分配；须人工核实并清理容器后再释放预留。
