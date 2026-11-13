# 后端接口

模块四增加执行字段 `keep_warm`、池查询与维护接口，见 [模块四开发与验收](../docs/module4_implementation.md)。

第二阶段中，`keep_warm=true` 同时启用 Active 整批排队策略。必须使用不同 task_id 并发提交。进度 stage 可为 `waiting_for_instance` 或 `queue_fallback`，waiting_ms 来自服务端实际经过时间；plan.dispatch 提供候选动作、实际等待和准备/清理成本。排队时未新增硬件预留，正式交接后才取得新租约。Restore 尚未实现。

执行进度接口：`POST /api/v1/executions` 提交 `{task, frames}` 并返回 202 与 job_id；`GET /api/v1/executions/{job_id}` 查询真实事件与分配方案；完成后 `GET /api/v1/executions/{job_id}/result` 获取结果。现有同步接口仍可使用。任务记录仅保存在当前后端进程内，最多保留 32 项，事件保留最近 256 条，服务重启后清空，不提供取消或跨进程恢复。

`api/` 提供 HTTP 接口，`services/` 组织任务与资源用例，`repositories/` 读取 JSON 文件。任务表征、组件匹配、资源规划和调度分别由对应模块实现。设置 `MRF_DEPLOYMENT_CONFIG` 后，`POST /api/v1/deployments/execute` 使用新规格 DAG 的物理放置与 Docker 容器执行；配置和接口见 [deployment/README.md](../deployment/README.md)。原有调度仿真接口仍不部署容器。

在项目根目录使用同一个 Python 环境安装依赖并启动服务：

```powershell
python -m pip install -r requirements.txt
python -m uvicorn backend.api.app:app --reload
```

默认地址为 `http://127.0.0.1:8000`，接口文档位于 `/docs`。`MRF_TASK_DIR` 和 `MRF_RESOURCE_DIR` 可指定其他 JSON 目录；文件名去掉 `.json` 后作为查询名称。

任务 JSON 清单接口不等于真实数据集接口。原有七个业务 JSON 定位为合成接口测试；新下载的信号、标签与来源记录放在 [datasets](../datasets/README.md)，当前尚无自动数据集导入器。

| 方法 | 路径 | 功能 |
| --- | --- | --- |
| GET | `/api/v1/health` | 健康检查 |
| GET | `/api/v1/tasks` | 列出任务文件 |
| GET | `/api/v1/tasks/{name}` | 读取任务 JSON |
| POST | `/api/v1/tasks/validate` | 校验细化任务 |
| POST | `/api/v1/task-representations/external` | 拆解单条原始射频请求、请求数组或 `{ "tasks": [...] }` 合集，生成五元组业务表征及公共功能模板 |
| GET | `/api/v1/task-representations/template` | 下载空白 `S/Q/R/G/X` 五元组任务模板 |
| POST | `/api/v1/task-representations/normalized` | 校验并接收已填写的 `rf_task_representation.v1` 业务任务 |
| POST | `/api/v1/task-representations/from-template` | 接收 `task_template` 和 `task_id`，恢复指定公共模板变体 |
| POST | `/api/v1/tasks/{name}/match` | 匹配已保存的细化任务 |
| POST | `/api/v1/matches` | 匹配提交的细化任务 |
| POST | `/api/v1/plans` | 预览归一化资源规划 |
| GET | `/api/v1/deployments/resources` | 查看物理处理节点容量、当前预留和活动容器实际统计；未配置部署时返回 `configured: false` |
| POST | `/api/v1/deployments/execute` | 对已细化匹配的业务任务提交并执行容器链 |
| POST | `/api/v1/simulations/schedule` | 仿真容量预留与实例激活 |
| POST | `/api/v1/simulations/tasks/{task_id}/complete` | 将实例转为 Warm 状态 |
| GET | `/api/v1/simulations/state` | 查看任务及实例状态 |
| GET | `/api/v1/resources` | 列出资源文件 |
| GET | `/api/v1/resources/{name}` | 读取资源 JSON |

原始 `external_rf_task.v1` 请求不需要预先填写 `S/Q/R/G/X` 或功能 DAG。表征模块按业务规则生成它们，匹配与规划接口也可直接接收该原始请求。已生成的 `rf_task_representation.v1` 五元组可导入复查；公共模板需先通过 `/task-representations/from-template` 恢复指定任务。`TaskService.execute(document, sources)` 在进程内执行已校验的旧版 DAG；`execute_with_schedule` 还会预留仿真容量，并在成功时转为 Warm、失败时释放。容器执行接口接收业务任务与按入口节点组织的数据包帧，详见 [部署说明](../deployment/README.md)。资源 JSON 当前作为独立清单读取，不会自动转换为仿真调度容量。
