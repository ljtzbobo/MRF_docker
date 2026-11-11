"""Strategy results; feasibility and cost belong to PlacementProblem."""

from dataclasses import dataclass


@dataclass
class SearchResult:
    assignment: dict | None
    explored: int
    exhaustive: bool
    limited: bool = False
    unsupported: bool = False
    issues: tuple[str, ...] = ()


def search_exact(problem, max_assignments):
    best = None
    best_cost = float("inf")
    explored = 0
    limited = False
    reasons = set()

    def visit(index, assignment):
        nonlocal best, best_cost, explored, limited
        if index == len(problem.order):
            cost = problem.evaluate(assignment, complete=True)[0]
            if cost < best_cost:
                best, best_cost = dict(assignment), cost
            return
        node = problem.order[index]
        for option in problem.options[node]:
            if explored >= max_assignments:
                limited = True
                return
            explored += 1
            candidate = {**assignment, node: option}
            try:
                problem.evaluate(candidate)
            except ValueError as exc:
                reasons.add(str(exc))
                continue
            visit(index + 1, candidate)
            if limited:
                return

    visit(0, {})
    return SearchResult(best, explored, not limited, limited,
                        issues=tuple(sorted(reasons)) if best is None else ())
