# MRF 任务工作台

前端使用 Vue 3、TypeScript、Vite、Pinia、Vue Flow 和 ECharts。任务页上传原始 `external_rf_task.v1` 业务请求，任务表征模块根据任务类型、方向和业务规则生成 `S/Q/R/G/X`，再进入功能细化、组件匹配和资源规划。页面展示业务功能图、规格 DAG、组件执行图，以及物理处理节点容量、预留和活动容器实际统计。上传的文件在浏览器内读取并发送给后端分析，不写入任务目录。

先按 `deployment/README.md` 构建镜像和标定本机资源，再在项目根目录启动已配置的 FastAPI，然后在 `frontend/` 目录安装依赖并启动 Vite：

```powershell
.\deployment\start_local_api.ps1
cd frontend
npm install
npm run dev
```

浏览器访问 `http://127.0.0.1:5173/tasks`；资源页为 `/resources`，支持地址直达与浏览器前进后退。Vite 将 `/api` 转发到本机 8000 端口。未配置 `MRF_DEPLOYMENT_CONFIG` 时，容器资源页显示未配置状态；配置后每 4 秒读取一次容量、调度预留和活动容器 Docker 统计。实际 CPU 与内存趋势按当前页面的采样记录绘制，刷新页面后重新开始。后端仍以 JSON 传输结构化数据；前端将任务资源 `R` 展开为字段、单位及缺失状态，物理分配预览展示处理节点预留量。

若 8000 或 5173 已被占用，可以用 `.\deployment\start_local_api.ps1 -Port 8001` 启动 API，再在前端目录设置 `$env:MRF_API_TARGET='http://127.0.0.1:8001'` 并执行 `npm run dev -- --port 5174`。

上传文件可为单条原始请求、原始请求数组，或 `{ "tasks": [...] }` 合集。原始请求描述任务类型、方向、到达时间、业务参数、性能要求、数据入口和预期输出；不包含预编的 `G` 或 CID。合集上传后可按任务 ID 切换查看每条任务的表征和匹配结果。参考 `examples/BusinessRequests/External/07_spectrum_analysis.json` 和 `all_tasks.json`；未指定处理配置的任务会显示“待细化”及缺失条件，页面不会自行补造波形或组件参数。

已生成的 `rf_task_representation.v1` 五元组和公共模板仍可导入，用于复查或恢复已有表征；它们不是编写仿真任务的前置条件。`python -m examples.generate_data_link_upload` 生成可上传的数据链接收原始请求、表征及输入帧，位于 `public/samples/`。

输入帧是非空数组，每项以执行图的入口节点 ID 为键，值为 `deployment.protocol.encode_packet()` 格式的数据包。仅当容器部署已配置、任务为 `MATCHED`、物理分配为 `FEASIBLE_ESTIMATE` 且输入帧已通过校验时，页面才允许提交。后端会在提交时再次匹配和预览，处理期间资源状态可能变化。执行结束显示结果或错误，并刷新资源占用。
