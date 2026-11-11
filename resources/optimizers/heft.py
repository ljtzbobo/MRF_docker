"""Upward-rank HEFT variant for serial invocation with bounded alternative placement.

Ranks choose the next ready node; incremental serial cost chooses its host.
This does not use parallel earliest-finish time as a Docker latency estimate.
"""

from .common import SearchResult


def search_heft(problem, max_assignments):
    successors = {key: [] for key in problem.order}
    predecessors = {key: set() for key in problem.order}
    for source, target in problem.edges:
        successors[source].append(target)
        predecessors[target].add(source)
    ranks = {}
    for node in reversed(problem.order):
        mean = sum(problem.node_cost(option) for option in problem.options[node]) / len(problem.options[node])
        ranks[node] = mean + max((problem.average_transfer(node, target) + ranks[target]
                                 for target in successors[node]), default=0)
    order = []
    remaining = set(problem.order)
    while remaining:
        ready = [key for key in remaining if not predecessors[key] & remaining]
        node = min(ready, key=lambda key: (-ranks[key], key))
        order.append(node)
        remaining.remove(node)

    explored = 0
    limited = False
    reasons = set()

    def visit(index, assignment):
        nonlocal explored, limited
        if index == len(order):
            return assignment
        node = order[index]
        choices = []
        for sequence, option in enumerate(problem.options[node]):
            if explored >= max_assignments:
                limited = True
                break
            explored += 1
            candidate = {**assignment, node: option}
            try:
                cost = problem.evaluate(candidate)[0]
            except ValueError as exc:
                reasons.add(str(exc))
                continue
            choices.append((cost, sequence, candidate))
        for _, _, candidate in sorted(choices, key=lambda item: item[:2]):
            result = visit(index + 1, candidate)
            if result is not None:
                return result
            if limited:
                break
        return None

    assignment = visit(0, {})
    return SearchResult(assignment, explored, assignment is None and not limited, limited,
                        issues=tuple(sorted(reasons)) if assignment is None else ())
