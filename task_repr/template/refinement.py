"""Specification types and compatibility facade; composition belongs to module two."""

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class RefinementIssue:
    code: str
    detail: str
    node_id: str | None = None


@dataclass(frozen=True)
class SpecificationNode:
    node_id: str
    role: str
    capability: str
    inputs: dict[str, str]
    outputs: dict[str, str]
    parameters: dict[str, str | int | float | bool]
    business_node_id: str

    def to_dict(self) -> dict:
        return {"id": self.node_id, "business_node_id": self.business_node_id,
                "role": self.role, "capability": self.capability,
                "inputs": self.inputs, "outputs": self.outputs,
                "parameters": self.parameters}


@dataclass(frozen=True)
class SpecificationEdge:
    source: str
    source_port: str
    target: str
    target_port: str

    def to_dict(self) -> dict:
        return {"source": self.source, "source_port": self.source_port,
                "target": self.target, "target_port": self.target_port}


@dataclass(frozen=True)
class SpecificationGraph:
    nodes: tuple[SpecificationNode, ...]
    edges: tuple[SpecificationEdge, ...]
    boundaries: tuple[dict, ...]
    profile_id: str
    profile_version: str
    completion_scope: str
    proposed_contracts: dict[str, str]
    prerequisites: tuple[str, ...]
    covered_outputs: tuple[str, ...]
    uncovered_outputs: tuple[str, ...]

    def to_dict(self) -> dict:
        return {"nodes": [node.to_dict() for node in self.nodes],
                "edges": [edge.to_dict() for edge in self.edges],
                "boundaries": list(self.boundaries), "profile_id": self.profile_id,
                "profile_version": self.profile_version,
                "completion_scope": self.completion_scope,
                "proposed_contracts": self.proposed_contracts,
                "prerequisites": list(self.prerequisites),
                "covered_outputs": list(self.covered_outputs),
                "uncovered_outputs": list(self.uncovered_outputs)}


@dataclass(frozen=True)
class SpecificationSemantics:
    family: str
    task_type: str
    direction: str
    extension: dict[str, str | int | float | bool]


@dataclass(frozen=True)
class SpecificationTask:
    task_id: str
    S: SpecificationSemantics
    Q: dict
    R: dict
    G: SpecificationGraph
    X: dict


@dataclass(frozen=True)
class RefinementResult:
    task_id: str
    task: SpecificationTask | None
    issues: tuple[RefinementIssue, ...]


def refine_business_task(task: Mapping) -> RefinementResult:
    """Compatibility facade; implementation decisions belong to module two."""
    from matching.composition.planner import compose_business_task
    from matching.legacy_refinement import refine_business_task as legacy
    semantics = task.get('S', {})
    request = semantics.get('business_parameters', {})
    dynamic = (request.get('functional_dag') is not None or semantics.get('task_type') == 'spectrum_analysis'
        or any(name in semantics.get('expected_output', []) for name in ('speech_transcript', 'satellite_identity'))
        or not request.get('processing', {}).get('profile'))
    if not dynamic:
        return legacy(task)
    result = compose_business_task(task)
    return RefinementResult(result.task_id, result.specification_task,
        tuple(RefinementIssue(issue.code, issue.detail, issue.node_id) for issue in result.unresolved))
