"""已匹配规格 DAG 的放置、提交、容器执行与回收事务。"""

from dataclasses import dataclass
from dataclasses import asdict
from itertools import chain
from collections.abc import Sized
from typing import Iterable, Mapping

from components.runtime import DataPacket
from matching.matcher import StaticMatchResult
from resources.placement import PlacementPlan
from resources.measurements import PhysicalUsage
from scheduler import TaskScheduler
from task_executor import DAGExecutionError, DAGExecutor
from task_repr.template.refinement import SpecificationTask

from .docker_runtime import DockerCleanupError
from .inventory import component_inventory
from .pool import GlobalContainerPool


class DeploymentError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeploymentResult:
    task_id: str
    plan: PlacementPlan
    execution_graph: dict
    frame_outputs: tuple[dict[str, DataPacket], ...]
    execution_mode: str = "docker"


class DeploymentManager:
    def __init__(self, scheduler: TaskScheduler, runtime):
        if scheduler.placement_planner is None:
            raise ValueError("deployment requires a placement planner")
        self.scheduler = scheduler
        self.runtime = runtime
        self.pool = GlobalContainerPool(scheduler, runtime)
        from .dynamic import DynamicScheduler
        self.dynamic = DynamicScheduler(self)

    def execute(self,task,match,frames,fixed_nodes=None,**options):
        if not isinstance(task,SpecificationTask):
            return self._execute(task,match,frames,fixed_nodes,**options)
        self.dynamic.begin(task)
        try:return self._execute(task,match,frames,fixed_nodes,**options)
        finally:self.dynamic.end(task.task_id)

    def resource_snapshot(self) -> dict:
        """提供物理容量、调度预留和活动容器统计。"""
        planner = self.scheduler.placement_planner
        # Keep lifecycle and ledger fields from the same instant; runtime stats may be newer.
        with self.scheduler._lock:
            state = self.scheduler.placement_snapshot()
            pool = self.pool.snapshot()
            breakdown = {}
            for node_id in planner.nodes:
                background = asdict(self.scheduler.placement_baseline.node_used[node_id]
                    if self.scheduler.placement_baseline else PhysicalUsage())
                warm = asdict(PhysicalUsage())
                for record in self.scheduler.placement_instances.values():
                    if record.state == 'WARM' and record.processing_node_id == node_id:
                        resident = record.resident_demand or PhysicalUsage(memory_bytes=record.memory_bytes)
                        for key, value in asdict(resident).items():
                            warm[key] += value
                active = {key: value - background[key] - warm[key]
                          for key, value in state['node_used'][node_id].items()}
                breakdown[node_id] = dict(background=background, active=active, warm=warm)
        actual = self.runtime.snapshot() if hasattr(self.runtime, "snapshot") else {}
        pooled = {item['instance_id']: item for item in pool['instances']}
        return {
            "configured": True,
            "execution_mode": getattr(self.runtime, "execution_mode", "docker"),
            "nodes": [{"node_id": node_id, "hardware_id": node.hardware.hardware_id,
                       "device_id": node.device_id, "processor": node.processor,
                       "unit_index": node.unit_index,
                       "capacity": asdict(node.hardware.capacity),
                       "reserved_breakdown": breakdown[node_id],
                       "available": {key: value - state["node_used"][node_id][key]
                                     for key, value in asdict(node.hardware.capacity).items()},
                       "reserved": state["node_used"][node_id]}
                      for node_id, node in planner.nodes.items()],
            "active_tasks": state["active_tasks"],
            "component_templates": component_inventory(planner, self.runtime),
            "idle_container_pool_supported": hasattr(self.runtime, "reset"),
            "pool": pool,
            "inventory_scope": "current_backend_managed_instances_and_configured_profiles",
            "containers": [{**item, **pooled.get(item["container_unit_id"], {}),
                            "actual": actual.get(item["container_unit_id"])}
                           for item in state["instances"] if item["state"] != "RELEASED"],
        }

    def _execute(self, task: SpecificationTask, match: StaticMatchResult,
                frames: Iterable[Mapping[str, DataPacket]],
                fixed_nodes: dict[str, str] | None = None, *, algorithm="exact",
                frame_count: int | None = None, max_assignments=100000, progress=None,
                keep_warm: bool = False) -> DeploymentResult:
        def notify(event, **details):
            if progress is not None:
                try:
                    progress({"event": event, **details})
                except Exception:
                    pass  # Monitoring must not interrupt execution or resource cleanup.
        notify('planning')
        if not isinstance(task, SpecificationTask) or match.status != "MATCHED" or not match.execution_graph:
            raise DeploymentError("deployment requires a fully matched specification DAG")
        executor = DAGExecutor.from_match(task, match)
        graph = match.execution_graph
        # 有长度的批次直接计数；生成器可显式声明帧数以保持流式消费。
        if frame_count is None:
            if not isinstance(frames, Sized):
                frames = tuple(frames)
            frame_count = len(frames)
        if type(frame_count) is not int or frame_count < 1:
            raise DeploymentError("deployment requires at least one input frame")
        frames = iter(frames)
        try:
            first_frame = next(frames)
        except StopIteration as exc:
            raise DeploymentError("deployment requires at least one input frame") from exc
        options = dict(algorithm=algorithm, frame_count=frame_count, max_assignments=max_assignments)
        if type(keep_warm) is not bool:
            raise DeploymentError("keep_warm must be a boolean")
        pooled = keep_warm or self.pool.journal is not None
        if pooled:
            plan = self.pool.reserve(task, match, fixed_nodes, notify=notify, **options)
        else:
            preview = self.scheduler.preview_placement(task, match, fixed_nodes, **options)
            if preview.status != "FEASIBLE_ESTIMATE":
                raise DeploymentError(f"{preview.status}: {'; '.join(preview.issues)}")
            self.runtime.preflight(preview.allocations)
            plan = self.scheduler.submit_placement(task, match, fixed_nodes, **options)
        notify('planned', plan=plan.to_dict(), total_frames=frame_count)
        handles = {}
        failure = None
        result = None
        try:
            if pooled:
                self.runtime.preflight(plan.allocations)
            nodes = {node["id"]: node for node in graph["nodes"]}
            allocations = {item.node_id: item for item in plan.allocations}
            if set(nodes) != set(allocations) or any(
                    nodes[key]["cid"] != item.cid for key, item in allocations.items()):
                raise DeploymentError("placement bindings differ from the execution graph")
            for node_id in graph["topological_order"]:
                notify('starting_node', node_id=node_id)
                if pooled:
                    handles[node_id] = self.pool.start(allocations[node_id], nodes[node_id])
                    if handles[node_id].acquisition != allocations[node_id].acquisition:
                        plan=self.pool.batches[task.task_id]['plan']
                        allocations={a.node_id:a for a in plan.allocations}
                    notify('instance_acquired', node_id=node_id,
                           instance_id=allocations[node_id].container_unit_id,
                           acquisition=allocations[node_id].acquisition, plan=plan.to_dict())
                else:
                    handles[node_id] = self.runtime.start(allocations[node_id], nodes[node_id])
                notify('node_ready', node_id=node_id)

            def docker_invoke(node_id, packets):
                notify('processing_node', node_id=node_id, frame_index=frame_index)
                try:
                    result = (self.pool if pooled else self.runtime).process(handles[node_id], packets)
                except Exception as exc:
                    notify('node_failed', node_id=node_id, error=str(exc))
                    raise
                notify('node_completed', node_id=node_id, frame_index=frame_index)
                return result

            outputs = []
            for frame_index, frame in enumerate(chain((first_frame,), frames), 1):
                if pooled and frame_index > 1:
                    try:
                        updated = self.dynamic.boundary(task,match,plan,nodes,frame_count-frame_index+1,algorithm,notify)
                    except Exception as exc:
                        self.dynamic._record(task.task_id,'failed',[],error=str(exc))
                        notify('rebind_skipped',reason=str(exc))
                        updated = plan
                    if updated is not plan:
                        plan = updated
                        allocations = {a.node_id:a for a in plan.allocations}
                        handles = dict(allocations)
                if len(outputs) >= frame_count:
                    raise DeploymentError("input frame count differs from placement estimate")
                try:
                    outputs.append(executor._execute_frame(frame, docker_invoke).sink_outputs)
                    if pooled:
                        self.pool.frame_completed(task.task_id)
                    notify('frame_completed', frame_index=frame_index)
                except DAGExecutionError as exc:
                    raise DeploymentError(str(exc)) from exc
            if len(outputs) != frame_count:
                raise DeploymentError("input frame count differs from placement estimate")
            result = DeploymentResult(task.task_id, plan, graph, tuple(outputs),
                                      getattr(self.runtime, "execution_mode", "docker"))
        except BaseException as exc:
            failure = exc
        cleanup_errors = []
        notify('cleanup')
        if pooled:
            try:
                self.pool.finish(plan, success=failure is None, notify=notify, retain_warm=keep_warm)
            except Exception as exc:
                cleanup_errors.append(str(exc))
        else:
            for handle in reversed(list(handles.values())):
                try:
                    self.runtime.stop(handle)
                except Exception as exc:
                    cleanup_errors.append(str(exc))
        if not cleanup_errors and not isinstance(failure, DockerCleanupError):
            if not pooled:
                self.scheduler.complete_placement(task.task_id)
            notify('resources_released')
        if isinstance(failure, DockerCleanupError):
            raise DeploymentError(
                "container startup cleanup failed; placement remains reserved: " + str(failure)
            ) from failure
        if cleanup_errors:
            raise DeploymentError("container cleanup failed; placement remains reserved: " +
                                  "; ".join(cleanup_errors)) from failure
        if failure is not None:
            raise failure
        return result
