# 真实快照、对照实验与持久化接管

## 运行边界

`deployment/checkpoints.py` 使用原生 `docker checkpoint create`、`docker start --checkpoint`，保存的是空闲组件进程的 CRIU 状态。保存前重置并检查健康，确认容器停止后，全局池才释放 CPU/内存等运行预算。镜像、容器可写层及快照文件仍占磁盘。恢复后重新检查健康、重置组件，再接收新任务。它用于 clean Warm 模板恢复，不恢复执行中任务的帧位置、输入流或未完成结果。

快照绑定 Docker daemon ID、内核、架构、Engine 版本、镜像 ID、实现、处理节点、资源规格、参数和端口契约。租约以 SQLite 原子领取，一个快照不能同时被两个任务消费。恢复失败会使该快照失效；只有清理已确认且剩余时限允许时才冷启动回退。所有权、启动或删除结果不确定时隔离并保留预算。CPU、内存限制还会与实际 Docker HostConfig 核对。

调度动作依次考虑 Active、Warm、Restore、Cold，均经过模块三同一容量和时限检查。允许任务混用不同来源的实例。Restore 优先使用同配置近期实际准备耗时的最大观测值，缺样本时明确使用画像初始化成本；不能据此承诺硬实时。实时任务重绑定仍是第三阶段的软件对象重绑定，不是 CRIU 迁移。

## 本机独立实验环境

Desktop Engine 28.0.4 未包含 CRIU。Ubuntu 24.04 仓库的 Docker 29.1.3 在本机可保存快照，但桥接网络恢复报 `bind-mount /proc/0/ns/net`；原始失败证据保留在 `outputs/snapshot-benchmark.json`。该错误与 [Moby #50750](https://github.com/moby/moby/issues/50750) 一致。

实验使用另外安装的 Docker 官方 27.5.1、独立 containerd 和自行编译的 CRIU 4.2.1。CRIU 来源为 [官方 v4.2.1](https://github.com/checkpoint-restore/criu/releases/tag/v4.2.1)，源码归档 SHA256 为 `feffdf4638125ebb12d2434754f80a1d7bbba85a3e6bee98c216f88fb99a5d96`。通过 `criu check`。版本固定用于兼容性实验；生产部署需要单独验证维护版本与安全更新。

专用 socket 为 `/run/mrf-checkpoint27/docker.sock`；Docker 数据目录 `/var/lib/mrf-checkpoint-docker27`，containerd 数据目录 `/var/lib/mrf-checkpoint-containerd27`，桥为 mrfbr27（172.29.251.1/24）。VFS 存储不代表生产环境性能。没有切换 Windows Docker context。Desktop 中原有 neo4j、qdrant 不由这个实验服务管理。

在 Ubuntu 内以 root 运行以下命令。服务脚本依赖已经安装在 `/opt/mrf-checkpoint/` 下的上述二进制；它不会自动下载或安装依赖。

```sh
cd /mnt/f/small_artical/MRF_docker
sh deployment/start_checkpoint_engine.sh
export DOCKER_HOST=unix:///run/mrf-checkpoint27/docker.sock
export PATH=/opt/mrf-checkpoint/docker-27.5.1/docker:$PATH
docker info
```

`systemd-run` 创建的是临时服务，不自动跨 WSL 重启开机启动。服务日志通过 `journalctl -u mrf-checkpoint-engine27` 和 `journalctl -u mrf-checkpoint-containerd27` 查看。再次运行脚本前，若服务曾失败，需要先检查日志并清除对应 failed 状态。不要对 Desktop 或其他容器执行清理命令。

镜像可以按标准 `deployment/Dockerfile` 构建。当前实验为复用已有 NumPy 镜像，使用 `deployment/Dockerfile.checkpoint` 离线构建：先向 `deployment/checkpoint-wheels/` 下载 Linux CPython 3.11 的 jsonschema 及依赖 wheel，再以 `mrf-component:local` 为 BASE。wheel 为本机缓存，已被 Git 忽略。标准 Dockerfile 也已补齐 jsonschema 运行依赖。

## 实验命令与统计口径

```sh
python3 -m examples.snapshot_benchmark --repetitions 5 --output outputs/snapshot-benchmark27.json
python3 -m examples.pool_takeover_demo --folder outputs/pool-takeover27
```

第一条对同一镜像、C03 DDC 参数、1000m CPU/256MiB 限额、4096 个合成 IQ 样本做五轮随机动作顺序的冷启动/Warm/Restore 比较。输入为合成接口数据，不宣称是真实射频数据集。三种方式对同一输入的输出 SHA256 必须相同。

准备墙钟时间包含 Docker 操作、健康检查及重置。冷启动已有本地镜像，不含拉取和构建。Warm 的首次种子启动、Restore 的种子启动与快照创建不计入恢复准备窗口；报告另外记录 Warm 种子启动、每次快照创建时间。cgroup v2 CPU 计数含探针开销；根据计数文件 inode 检测恢复后的 cgroup 重建，属于近似的容器 CPU 观测，不含宿主 daemon/CRIU 全部开销。内存为当前 cgroup 用量，非峰值。中位数仅取成功且清理成功的样本，失败不计为零，也不以冷启动冒充恢复。

第二条使用四个独立 Python 进程：创建 Warm 后直接退出；新进程接管同一个真实容器并 Warm 复用、保存快照；第三个进程由全局池分配 Restore 并执行，再留下一个中断的 Active 任务直接退出；第四个进程验证隔离、预算保留、旧租约失效并确认清理。准入画像为明确标记的合成画像，实验用于生命周期正确性，不用于硬件性能标定。

## 本机五轮结果（2026-11-13）

原始报告为 `outputs/snapshot-benchmark27.json`。三种方式各 5/5 成功，15 次输出 SHA256 完全一致。

| 动作 | 准备墙钟中位数 | 容器准备 CPU 观测中位数 | 就绪内存中位数 |
| --- | ---: | ---: | ---: |
| 冷启动 | 8296.13 ms | 830.73 ms | 33.68 MiB |
| Warm 复用 | 8.46 ms | 48.78 ms | 33.20 MiB |
| CRIU 恢复 | 13562.00 ms | 209.27 ms | 32.03 MiB |

本机恢复比冷启动慢约 63.5%；准备 CPU 容器内计数较低，但没有包含宿主 CRIU、daemon 和文件系统全部开销，不能解释成系统总 CPU 节约。本次验证了真实恢复正确性，未达到恢复提速目标。VFS、WSL、具体组件初始化负担和实验期间宿主负载都限制了结果外推。Warm 在该工作负载下的准备时间明显较短。换生产存储与更重初始化组件后，应重新实验，不能预设恢复一定比冷启动快。

`outputs/pool-takeover27/report.json` 的四个跨进程阶段全部通过：真实 Warm 接管、Warm 复用后保存、全局池 Restore 执行、中断 Active 的预算保留和确认清理。旧租约拒绝还由持久化单元测试覆盖。

## 启用持久化

Docker 部署 JSON 在原配置基础上增加可选字段（相对路径相对于部署 JSON）：

```json
{"checkpoint_store":"../outputs/pool-checkpoints.sqlite", "pool_state":"../outputs/pool-state.sqlite"}
```

也可设置 `MRF_POOL_STATE` 指定后端状态日志。启动时先检查独占文件锁，再核对配置及 daemon ID，逐个查询 Docker 实际状态、镜像、标签和配额。健康的 Warm 重置后接管；不存在的对象只有在 daemon 确认不存在后才解除占用；中断的 Active 不自动重放，隔离并保留原任务的完整预留，新租约拒绝旧调用，可经 `retry-cleanup` 确认清理。模拟后端的 Python 对象不能跨进程存活，重启记录为丢失，不伪造 Warm。

日志写失败会停止新任务准入。日志与快照元数据分别为原子 SQLite 事务，跨文件没有分布式事务；异常窗口中孤立的 creating/claimed 快照保持不可分配，需要对照 daemon 和日志人工核对，不能直接强制改为 available。当前仅支持一个主机/操作系统上的单所有者接管；不承诺 Windows/WSL 混用锁、分布式高可用、输入流自动恢复或进程内仿真上下文续跑。

启用持久化时，即使请求 `keep_warm=false`，任务也经全局池预留并写入日志，结束后按请求销毁实例；不能通过关闭 Warm 保留绕过崩溃接管和资源账本。

## 接口与资源页

- `GET /api/v1/pool`：快照列表、持久化状态和接管摘要。
- `POST /api/v1/pool/instances/{instance_id}/checkpoint`：仅保存未认领的 READY Warm。
- `POST /api/v1/pool/checkpoints/{checkpoint_id}/remove`：删除可用/失效快照，核对实际来源后移除。
- `POST /api/v1/pool/tasks/{task_id}/retry-cleanup`：确认清理重启隔离的 Active。

资源页显示保存/移除快照及接管结果；未配置快照库的仿真页面明确提示不支持。模块三和模块四仍通过统一资源账本分配，保存快照不会绕过预算或产生真实 DSP/FPGA 设备。
