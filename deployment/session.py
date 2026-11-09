"""已匹配规格 DAG 的放置、提交、容器执行与回收事务。"""

from dataclasses import dataclass
from dataclasses import asdict
from itertools import chain
from typing import Iterable, Mapping

from components.runtime import DataPacket
from matching.contracts import validate_runtime_packet, validate_signal_packet
from matching.matcher import StaticMatchResult
from components.specs import load_protocol_specs
from resources.placement import PlacementPlan
from scheduler import TaskScheduler
from task_executor import DAGExecutor
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
                fixed_nodes: dict[str, str] | None = None) -> DeploymentResult:
        if not isinstance(task, SpecificationTask) or match.status != "MATCHED" or not match.execution_graph:
            raise DeploymentError("deployment requires a fully matched specification DAG")
        DAGExecutor.from_match(task, match)
        graph = match.execution_graph
        frames = iter(frames)
        try:
            first_frame = next(frames)
        except StopIteration as exc:
            raise DeploymentError("deployment requires at least one input frame") from exc
        preview = self.scheduler.preview_placement(task, match, fixed_nodes)
        if preview.status != "FEASIBLE_ESTIMATE":
            raise DeploymentError(f"{preview.status}: {'; '.join(preview.issues)}")
        self.runtime.preflight(preview.allocations)
        plan = self.scheduler.submit_placement(task, match, fixed_nodes)
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
            inputs = {node_id: [] for node_id in nodes}
            for edge in graph["edges"]:
                inputs[edge["target"]].append(edge)
            source_boundaries = [item for item in graph["boundaries"]
                                 if item["direction"] == "source"]
            roots = {item["node_id"]: item for item in source_boundaries}
            if len(roots) != len(source_boundaries):
                raise DeploymentError("deployment requires one source port per root node")
            sinks = {item["node_id"]: item for item in graph["boundaries"]
                     if item["direction"] == "sink"}
            task_nodes = {node.node_id: node for node in task.G.nodes}
            outputs = []
            protocols = load_protocol_specs()
            for frame in chain((first_frame,), frames):
                if not isinstance(frame, Mapping) or set(frame) != set(roots):
                    raise DeploymentError("frame must supply every source boundary")
                produced = {}
                for node_id in graph["topological_order"]:
                    node = nodes[node_id]
                    packets = ({roots[node_id]["port"]: frame[node_id]} if node_id in roots else
                               {edge["target_port"]: produced[edge["source"]]
                                for edge in inputs[node_id]})
                    if set(packets) != set(node["inputs"]):
                        raise DeploymentError(f"{node_id} has incomplete input dependencies")
                    for port, packet in packets.items():
                        error = validate_runtime_packet(node["inputs"][port], packet, protocols=protocols)
                        error = error or validate_signal_packet(task, task_nodes[node_id],
                            packet, "input", match.bindings[node_id])
                        if error:
                            raise DeploymentError(f"{node_id}.{port}: {error}")
                    output = self.runtime.process(handles[node_id], packets)
                    output_port, format_name = next(iter(node["outputs"].items()))
                    error = validate_runtime_packet(format_name, output, protocols=protocols)
                    error = error or validate_signal_packet(task, task_nodes[node_id],
                        output, "output", match.bindings[node_id])
                    if error:
                        raise DeploymentError(f"{node_id}.{output_port}: {error}")
                    produced[node_id] = output
                outputs.append({node_id: produced[node_id] for node_id in sinks})
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
