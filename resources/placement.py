"""固定组件 DAG 在处理节点上的初始分配与有界优化搜索。"""

from dataclasses import asdict, dataclass, replace
from copy import deepcopy
import math

from matching.matcher import StaticMatchResult
from task_repr.models.task import TaskRepresentation
from task_repr.template.refinement import SpecificationTask

from .measurements import (MeasurementCatalog, execution_profile_basis,
                           PhysicalHardwareProfile, PhysicalUsage)
from .constraints import task_time_limit as _time_limit


_FIELDS = tuple(PhysicalUsage.__dataclass_fields__)


@dataclass(frozen=True)
class ProcessingNode:
    node_id: str
    hardware: PhysicalHardwareProfile
    device_id: str | None = None
    processor: str | None = None
    unit_index: int | None = None


@dataclass(frozen=True)
class NetworkLink:
    source: str
    target: str
    capacity_bytes_per_s: float
    latency_ms: float

    def __post_init__(self):
        if (not self.source or not self.target or self.source == self.target or
                not math.isfinite(self.capacity_bytes_per_s) or self.capacity_bytes_per_s <= 0 or
                not math.isfinite(self.latency_ms) or self.latency_ms < 0):
            raise ValueError("network link needs distinct endpoints, positive rate and nonnegative latency")


@dataclass(frozen=True)
class EdgeTransfer:
    source: str
    target: str
    bytes_per_call: int
    reserved_bytes_per_s: float

    def __post_init__(self):
        if (not self.source or not self.target or type(self.bytes_per_call) is not int or
                self.bytes_per_call < 0 or not math.isfinite(self.reserved_bytes_per_s) or
                self.reserved_bytes_per_s <= 0):
            raise ValueError("edge transfer needs nonnegative bytes and positive reserved rate")


@dataclass(frozen=True)
class ReusableInstance:
    instance_id: str
    processing_node_id: str
    cid: str
    implementation_id: str
    demand: PhysicalUsage
    resident_demand: PhysicalUsage
    acquisition: str = "warm_reuse"
    setup_ms: float | None = None
    cost_basis: str = "profile_setup_fallback"


@dataclass(frozen=True)
class PlacementState:
    node_used: dict[str, PhysicalUsage]
    link_used: dict[tuple[str, str], float]
    # Candidates have already passed strict runtime/configuration compatibility checks.
    reuse_candidates: dict[str, tuple[ReusableInstance, ...]] | None = None
    required_reuse: dict[str, str] | None = None
    cold_setup_costs: dict | None = None


@dataclass(frozen=True)
class PlacementAllocation:
    node_id: str
    cid: str
    implementation_id: str
    container_unit_id: str
    processing_node_id: str
    processor: str
    demand: PhysicalUsage
    runtime_p95_ms: float
    setup_p95_ms: float
    device_id: str | None = None
    core_id: int | None = None
    region_id: int | None = None
    lease_id: str | None = None
    acquisition: str = "cold_start"
    runtime_identity: str | None = None
    estimated_acquisition_ms: float | None = None
    acquisition_cost_basis: str = "profile_setup"

    def to_dict(self) -> dict:
        return {**asdict(self), "demand": asdict(self.demand)}


@dataclass(frozen=True)
class PlacementPlan:
    task_id: str
    status: str
    allocations: tuple[PlacementAllocation, ...]
    node_demand: dict[str, PhysicalUsage]
    link_demand: dict[tuple[str, str], float]
    edge_transfer_ms: dict[tuple[str, str], float]
    estimated_completion_ms: float | None
    time_limit_ms: float | None
    issues: tuple[str, ...]
    explored: int = 0
    exhaustive: bool = True
    algorithm: str = "exact"
    optimality_proven: bool = False
    frame_count: int = 1
    estimation_basis: str = "serial_setup_plus_frames_times_process_and_transfer; sum_of_profile_statistics_not_task_p95"
    profile_basis: str = "unspecified"
    dispatch: dict | None = None

    def to_dict(self) -> dict:
        return {"task_id": self.task_id, "status": self.status,
                "unit_system": "physical", "allocations": [item.to_dict() for item in self.allocations],
                "node_demand": {key: asdict(value) for key, value in self.node_demand.items()},
                "link_demand": {f"{a}->{b}": value for (a, b), value in self.link_demand.items()},
                "edge_transfer_ms": {f"{a}->{b}": value for (a, b), value in self.edge_transfer_ms.items()},
                "estimated_completion_ms": self.estimated_completion_ms,
                "time_limit_ms": self.time_limit_ms, "issues": list(self.issues),
                "explored": self.explored, "exhaustive": self.exhaustive,
                "algorithm": self.algorithm, "optimality_proven": self.optimality_proven,
                "frame_count": self.frame_count, "estimation_basis": self.estimation_basis,
                "profile_basis": self.profile_basis, "dispatch": deepcopy(self.dispatch)}


def _add(left: PhysicalUsage, right: PhysicalUsage) -> PhysicalUsage:
    return PhysicalUsage(**{key: getattr(left, key) + getattr(right, key) for key in _FIELDS})


def _fits(demand: PhysicalUsage, available: PhysicalUsage) -> bool:
    return all(getattr(demand, key) <= getattr(available, key) for key in _FIELDS)


def _order(task) -> tuple[str, ...]:
    ids = {node.node_id for node in task.G.nodes}
    if not ids or len(ids) != len(task.G.nodes):
        raise ValueError("DAG must have unique nonempty nodes")
    incoming = {key: 0 for key in ids}
    outgoing = {key: [] for key in ids}
    seen = set()
    for edge in task.G.edges:
        pair = (edge.source, edge.target)
        if edge.source not in ids or edge.target not in ids or pair in seen:
            raise ValueError("DAG has an invalid or duplicate dependency")
        seen.add(pair)
        incoming[edge.target] += 1
        outgoing[edge.source].append(edge.target)
    ready = sorted(key for key, count in incoming.items() if count == 0)
    order = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for target in outgoing[current]:
            incoming[target] -= 1
            if incoming[target] == 0:
                ready.append(target)
                ready.sort()
    if len(order) != len(ids):
        raise ValueError("task graph must be acyclic")
    return tuple(order)


@dataclass
class PlacementProblem:
    task: object
    match: StaticMatchResult
    state: PlacementState
    order: tuple[str, ...]
    options: dict
    nodes: dict
    links: dict
    transfers: dict
    edges: tuple
    frame_count: int

    def reused_instances(self, assignment):
        selected = {}
        occupied = set()
        required_bindings = self.state.required_reuse or {}
        for node_id in self.order:
            if node_id not in assignment:
                continue
            host, measurement = assignment[node_id]
            for item in (self.state.reuse_candidates or {}).get(node_id, ()):
                required = required_bindings.get(node_id)
                if ((required is None or item.instance_id == required)
                        and (item.instance_id not in required_bindings.values() or item.instance_id == required)
                        and item.instance_id not in occupied and item.processing_node_id == host
                        and item.cid == measurement.cid
                        and item.implementation_id == measurement.implementation_id
                        and item.demand == measurement.demand):
                    selected[node_id] = item
                    occupied.add(item.instance_id)
                    break
        return selected

    def evaluate(self, assignment, *, complete=False):
        """Validate partial or complete assignments using the same reservation rules."""
        if complete and set(assignment) != set(self.order):
            raise ValueError("assignment does not cover the whole DAG")
        demands = {key: PhysicalUsage() for key in self.nodes}
        link_demand = {key: 0.0 for key in self.links}
        edge_ms = {}
        setup = process = 0.0
        credits = {}
        reused = self.reused_instances(assignment)
        for node_id, identifier in (self.state.required_reuse or {}).items():
            if node_id in assignment and (node_id not in reused or reused[node_id].instance_id != identifier):
                raise ValueError(f"{node_id}: required reusable instance is unavailable")
        for item in reused.values():
            host = item.processing_node_id
            credits[host] = _add(credits.get(host, PhysicalUsage()), item.resident_demand)
        for node_id, option in assignment.items():
            if node_id not in self.options or option not in self.options[node_id]:
                raise ValueError(f"{node_id}: incompatible placement candidate")
            host, measurement = option
            demands[host] = _add(demands[host], measurement.demand)
            credit = credits.get(host)
            if credit is None:
                fits = _fits(_add(demands[host], self.state.node_used[host]), self.nodes[host].hardware.capacity)
            else:
                fits = all(getattr(self.state.node_used[host], key) - getattr(credit, key)
                           + getattr(demands[host], key) <= getattr(self.nodes[host].hardware.capacity, key)
                           for key in _FIELDS)
            if not fits:
                raise ValueError(f"{host}: node capacity exceeded by {node_id}")
            candidate = reused.get(node_id)
            cold_cost = (self.state.cold_setup_costs or {}).get(node_id, {}).get(
                (host, measurement.implementation_id), (measurement.effective_setup_ms, "profile_setup"))
            setup += (candidate.setup_ms if candidate is not None and candidate.setup_ms is not None
                      else cold_cost[0])
            process += measurement.runtime_p95_ms
        for source, target in self.edges:
            if source not in assignment or target not in assignment:
                continue
            left, source_measurement = assignment[source]
            right, target_measurement = assignment[target]
            transfer = self.transfers[(source, target)]
            if (transfer.bytes_per_call > source_measurement.output_bytes or
                    transfer.bytes_per_call > target_measurement.input_bytes):
                raise ValueError(f"{source}->{target}: transfer bytes exceed measured output or input")
            duration = 0.0
            if left != right:
                pair = (left, right)
                if pair not in self.links:
                    raise ValueError(f"{source}->{target}: no link {left}->{right}")
                link_demand[pair] += transfer.reserved_bytes_per_s
                link = self.links[pair]
                if link_demand[pair] + self.state.link_used[pair] > link.capacity_bytes_per_s:
                    raise ValueError(f"{left}->{right}: link capacity exceeded")
                duration = (link.latency_ms + transfer.bytes_per_call * 1000 /
                            transfer.reserved_bytes_per_s)
            edge_ms[(source, target)] = duration
        cost = setup + self.frame_count * (process + sum(edge_ms.values()))
        return cost, demands, link_demand, edge_ms

    def node_cost(self, option):
        measurement = option[1]
        return measurement.effective_setup_ms + self.frame_count * measurement.runtime_p95_ms

    def average_transfer(self, source, target):
        transfer = self.transfers[(source, target)]
        costs = []
        for left, _ in self.options[source]:
            for right, _ in self.options[target]:
                if left == right:
                    costs.append(0.0)
                elif (left, right) in self.links:
                    costs.append(self.frame_count * (self.links[(left, right)].latency_ms +
                                 transfer.bytes_per_call * 1000 / transfer.reserved_bytes_per_s))
        return sum(costs) / len(costs) if costs else 0.0


class PlacementPlanner:
    """Optimize fixed CID bindings; all strategies share physical feasibility and serial cost."""

    def __init__(self, nodes: tuple[ProcessingNode, ...], links: tuple[NetworkLink, ...],
                 measurements: MeasurementCatalog, transfers: tuple[EdgeTransfer, ...]):
        self.nodes = {item.node_id: item for item in nodes}
        self.links = {(item.source, item.target): item for item in links}
        self.measurements = measurements
        self.transfers = {(item.source, item.target): item for item in transfers}
        if (len(self.nodes) != len(nodes) or len(self.links) != len(links) or
                len(self.transfers) != len(transfers)):
            raise ValueError("processing nodes, links and transfers must have unique identifiers")
        if any(link.source not in self.nodes or link.target not in self.nodes for link in links):
            raise ValueError("network link endpoint is not a processing node")

    def plan(self, task: TaskRepresentation | SpecificationTask, match: StaticMatchResult,
             state: PlacementState, fixed_nodes: dict[str, str] | None = None,
             *, algorithm="exact", frame_count=1, max_assignments=100000) -> PlacementPlan:
        if algorithm not in ("exact", "chain_dp", "heft"):
            raise ValueError("unknown algorithm")
        for name, value in (("max_assignments", max_assignments), ("frame_count", frame_count)):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be positive")
        candidates = match.candidate_bindings or (match.bindings,)
        plans = []
        remaining = max_assignments
        for bindings in candidates:
            if remaining <= 0:
                break
            problem = self._prepare_problem(task, replace(match, bindings=bindings), state,
                                            fixed_nodes, frame_count)
            if isinstance(problem, PlacementPlan):
                candidate = replace(problem, algorithm=algorithm)
            else:
                from .optimizers import search_exact, search_chain_dp, search_heft
                search = {"exact": search_exact, "chain_dp": search_chain_dp, "heft": search_heft}[algorithm]
                outcome = search(problem, remaining)
                candidate = self._finalize_plan(problem, outcome, algorithm)
            plans.append(candidate)
            remaining -= candidate.explored
            if candidate.status == "SEARCH_LIMIT":
                break
        priority = {"FEASIBLE_ESTIMATE": 0, "UNVERIFIED_TIME": 1, "SEARCH_LIMIT": 2,
                    "HEURISTIC_FAILED": 3, "INFEASIBLE": 4, "NEEDS_CALIBRATION": 5,
                    "NEEDS_TRANSFER_PROFILE": 6, "UNSUPPORTED_GRAPH": 7}
        chosen = min(plans, key=lambda p: (priority[p.status],
                     p.estimated_completion_ms if p.estimated_completion_ms is not None else math.inf))
        truncated = (not match.candidates_exhaustive or len(plans) != len(candidates) or
                     any(p.status == "SEARCH_LIMIT" for p in plans))
        calibrated = all(p.status not in ("NEEDS_CALIBRATION", "NEEDS_TRANSFER_PROFILE") for p in plans)
        proven = (not truncated and calibrated and all(p.exhaustive for p in plans) and
                  chosen.optimality_proven)
        if truncated:
            return replace(chosen, status="SEARCH_LIMIT", exhaustive=False, optimality_proven=False,
                           explored=sum(p.explored for p in plans),
                           issues=chosen.issues + ("component or placement search limit reached",))
        return replace(chosen, explored=sum(p.explored for p in plans),
                       exhaustive=all(p.exhaustive for p in plans), optimality_proven=proven)

    def _prepare_problem(self, task, match, state, fixed_nodes, frame_count):
        order = _order(task)
        ids = set(order)
        edges = tuple((edge.source, edge.target) for edge in task.G.edges)
        if (match.task_id != task.task_id or match.status != "MATCHED" or match.unresolved or
                set(match.bindings) != ids or set(match.edges) != set(edges)):
            raise ValueError("placement requires the complete matched DAG")
        if fixed_nodes is not None and set(fixed_nodes) != ids:
            raise ValueError("fixed_nodes must assign every DAG node")
        if fixed_nodes is not None and set(fixed_nodes.values()) - set(self.nodes):
            raise ValueError("fixed_nodes references an unknown processing node")
        if set(state.node_used) != set(self.nodes) or set(state.link_used) != set(self.links):
            raise ValueError("placement state must cover every processing node and link")
        for key, used in state.node_used.items():
            if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0
                   for v in asdict(used).values()):
                raise ValueError(f"invalid resource state for {key}")
            if not _fits(used, self.nodes[key].hardware.capacity):
                raise ValueError(f"resource state exceeds capacity for {key}")
        for pair, used in state.link_used.items():
            if (type(used) not in (int, float) or not math.isfinite(used) or used < 0 or
                    used > self.links[pair].capacity_bytes_per_s):
                raise ValueError(f"invalid link state for {pair}")
        def diagnostic(status, issues):
            return PlacementPlan(task.task_id, status, (), {}, {}, {}, None, _time_limit(task),
                                 tuple(issues), frame_count=frame_count)
        missing = sorted(set(edges) - set(self.transfers))
        if missing:
            return diagnostic("NEEDS_TRANSFER_PROFILE", [f"missing edge transfer profile: {a}->{b}"
                                                        for a, b in missing])
        options = {}
        for key in order:
            binding = match.bindings[key]
            allowed = (fixed_nodes[key],) if fixed_nodes is not None else tuple(self.nodes)
            options[key] = tuple((host, item) for host in allowed
                                 for item in self.measurements.candidates(
                                     binding.cid, binding.parameters, self.nodes[host].hardware))
            required = (state.required_reuse or {}).get(key)
            if required:
                reusable = next((r for r in (state.reuse_candidates or {}).get(key, ())
                                 if r.instance_id == required), None)
                options[key] = tuple((host, item) for host, item in options[key]
                    if reusable is not None and host == reusable.processing_node_id
                    and item.cid == reusable.cid and item.implementation_id == reusable.implementation_id
                    and item.demand == reusable.demand)
        missing = [f"{key}: no compatible measurement or processing node for {match.bindings[key].cid}"
                   for key in order if not options[key]]
        if missing:
            return diagnostic("NEEDS_CALIBRATION", missing)
        return PlacementProblem(task, match, state, order, options, self.nodes, self.links,
                                self.transfers, edges, frame_count)

    def _finalize_plan(self, problem, outcome, algorithm):
        limit = _time_limit(problem.task)
        issues = list(outcome.issues)
        allocations = ()
        demands, links, transfers = {}, {}, {}
        duration = None
        basis = "unspecified"
        status = "UNSUPPORTED_GRAPH" if outcome.unsupported else (
            "SEARCH_LIMIT" if outcome.limited else
            "INFEASIBLE" if outcome.exhaustive else "HEURISTIC_FAILED")
        if outcome.assignment is not None:
            basis = execution_profile_basis(option[1] for option in outcome.assignment.values())
            # Recheck the complete graph independently of strategy and pruning decisions.
            duration, demands, links, transfers = problem.evaluate(outcome.assignment, complete=True)
            reused = problem.reused_instances(outcome.assignment)
            cold_costs = {key: (problem.state.cold_setup_costs or {}).get(key, {}).get(
                (outcome.assignment[key][0], outcome.assignment[key][1].implementation_id),
                (outcome.assignment[key][1].effective_setup_ms, "profile_setup")) for key in problem.order}
            allocations = tuple(PlacementAllocation(
                key, problem.match.bindings[key].cid, outcome.assignment[key][1].implementation_id,
                reused[key].instance_id if key in reused else f"{problem.task.task_id}:{key}", outcome.assignment[key][0],
                outcome.assignment[key][1].processor, outcome.assignment[key][1].demand,
                outcome.assignment[key][1].runtime_p95_ms, outcome.assignment[key][1].effective_setup_ms,
                device_id=problem.nodes[outcome.assignment[key][0]].device_id,
                core_id=problem.nodes[outcome.assignment[key][0]].unit_index if outcome.assignment[key][1].processor == "dsp" else None,
                region_id=problem.nodes[outcome.assignment[key][0]].unit_index if outcome.assignment[key][1].processor == "fpga" else None,
                acquisition=reused[key].acquisition if key in reused else "cold_start",
                estimated_acquisition_ms=reused[key].setup_ms if key in reused else cold_costs[key][0],
                acquisition_cost_basis=reused[key].cost_basis if key in reused else cold_costs[key][1])
                for key in problem.order)
            status = "UNVERIFIED_TIME" if limit is None else "FEASIBLE_ESTIMATE"
            if limit is None:
                issues.append("task has no explicit completion deadline")
            elif duration > limit or limit <= 0:
                issues.append("DAG completion time exceeds task time limit")
                status = "INFEASIBLE" if outcome.exhaustive else "HEURISTIC_FAILED"
            if outcome.limited:
                status = "SEARCH_LIMIT"
        if outcome.limited:
            issues.append("assignment search limit reached; optimum is unverified")
        return PlacementPlan(problem.task.task_id, status, allocations, demands, links, transfers,
                             duration, limit, tuple(issues), outcome.explored, outcome.exhaustive,
                             algorithm, outcome.exhaustive and outcome.assignment is not None,
                             problem.frame_count, profile_basis=basis)
