"""已匹配规格 DAG 的放置、提交、容器执行与回收事务。"""

from dataclasses import dataclass
from dataclasses import asdict
from itertools import chain
from collections.abc import Sized
from typing import Iterable, Mapping

from components.runtime import DataPacket
from matching.matcher import StaticMatchResult
from resources.placement import PlacementPlan
from scheduler import TaskScheduler
from task_executor import DAGExecutionError, DAGExecutor
from task_repr.template.refinement import SpecificationTask

from .docker_runtime import DockerCleanupError


class DeploymentError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeploymentResult:
    task_id: str
    plan: PlacementPlan
    execution_graph: dict
    frame_outputs: tuple[dict[str, DataPacket], ...]


class DeploymentManager:
    def __init__(self, scheduler: TaskScheduler, runtime):
        if scheduler.placement_planner is None:
            raise ValueError("deployment requires a placement planner")
        self.scheduler = scheduler
        self.runtime = runtime

    def resource_snapshot(self) -> dict:
        """提供物理容量、调度预留和活动容器统计。"""
        state = self.scheduler.placement_snapshot()
        planner = self.scheduler.placement_planner
        actual = self.runtime.snapshot() if hasattr(self.runtime, "snapshot") else {}
        return {
            "configured": True,
            "nodes": [{"node_id": node_id, "hardware_id": node.hardware.hardware_id,
                       "capacity": asdict(node.hardware.capacity),
                       "reserved": state["node_used"][node_id]}
                      for node_id, node in planner.nodes.items()],
            "active_tasks": state["active_tasks"],
            "containers": [{**item, "actual": actual.get(item["container_unit_id"])}
                           for item in state["instances"] if item["state"] != "RELEASED"],
        }

    def execute(self, task: SpecificationTask, match: StaticMatchResult,
                frames: Iterable[Mapping[str, DataPacket]],
                fixed_nodes: dict[str, str] | None = None, *, algorithm="exact",
                frame_count: int | None = None, max_assignments=100000) -> DeploymentResult:
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
        preview = self.scheduler.preview_placement(task, match, fixed_nodes, **options)
        if preview.status != "FEASIBLE_ESTIMATE":
            raise DeploymentError(f"{preview.status}: {'; '.join(preview.issues)}")
        self.runtime.preflight(preview.allocations)
        plan = self.scheduler.submit_placement(task, match, fixed_nodes, **options)
        handles = {}
        failure = None
        result = None
        try:
            nodes = {node["id"]: node for node in graph["nodes"]}
            allocations = {item.node_id: item for item in plan.allocations}
            if set(nodes) != set(allocations) or any(
                    nodes[key]["cid"] != item.cid for key, item in allocations.items()):
                raise DeploymentError("placement bindings differ from the execution graph")
            for node_id in graph["topological_order"]:
                handles[node_id] = self.runtime.start(allocations[node_id], nodes[node_id])

            def docker_invoke(node_id, packets):
                return self.runtime.process(handles[node_id], packets)

            outputs = []
            for frame in chain((first_frame,), frames):
                if len(outputs) >= frame_count:
                    raise DeploymentError("input frame count differs from placement estimate")
                try:
                    outputs.append(executor._execute_frame(frame, docker_invoke).sink_outputs)
                except DAGExecutionError as exc:
                    raise DeploymentError(str(exc)) from exc
            if len(outputs) != frame_count:
                raise DeploymentError("input frame count differs from placement estimate")
            result = DeploymentResult(task.task_id, plan, graph, tuple(outputs))
        except BaseException as exc:
            failure = exc
        cleanup_errors = []
        for handle in reversed(list(handles.values())):
            try:
                self.runtime.stop(handle)
            except Exception as exc:
                cleanup_errors.append(str(exc))
        if not cleanup_errors and not isinstance(failure, DockerCleanupError):
            self.scheduler.complete_placement(task.task_id)
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
