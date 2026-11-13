"""Exact chain DP with resource labels and safe Pareto dominance pruning."""

from dataclasses import dataclass

import numpy as np

from .common import SearchResult


@dataclass
class Label:
    assignment: dict
    cost: float
    resources: tuple
    boundary: tuple


class ParetoFrontier:
    """Group compatible boundaries and compare resource labels in vectorized batches."""

    def __init__(self, dimensions):
        self.labels = []
        self.size = 0
        self.resources = np.empty((32, dimensions))
        self.costs = np.empty(32)
        self.active = np.zeros(32, dtype=bool)

    def add(self, label):
        used = slice(0, self.size)
        vector = np.asarray(label.resources)
        # No rounding or tolerance: a label may only discard genuinely dominated states.
        if np.any(self.active[used] & (self.costs[used] <= label.cost) &
                  np.all(self.resources[used] <= vector, axis=1)):
            return
        removed = self.active[used] & (self.costs[used] >= label.cost) & np.all(
            self.resources[used] >= vector, axis=1)
        self.active[:self.size][removed] = False
        if self.size == len(self.costs):
            indices = np.flatnonzero(self.active[used])
            count = len(indices)
            self.resources[:count] = self.resources[indices]
            self.costs[:count] = self.costs[indices]
            self.labels = [self.labels[index] for index in indices]
            self.active[:] = False
            self.active[:count] = True
            self.size = count
            if count == len(self.costs):
                capacity = count * 2
                resources = np.empty((capacity, len(vector)))
                resources[:count] = self.resources[:count]
                self.resources = resources
                self.costs = np.concatenate((self.costs, np.empty(count)))
                self.active = np.concatenate((self.active, np.zeros(count, dtype=bool)))
        self.resources[self.size] = vector
        self.costs[self.size] = label.cost
        self.active[self.size] = True
        self.labels.append(label)
        self.size += 1

    def values(self):
        return [label for index, label in enumerate(self.labels) if self.active[index]]


def search_chain_dp(problem, max_assignments):
    order = problem.order
    if set(problem.edges) != set(zip(order, order[1:])):
        return SearchResult(None, 0, False, unsupported=True,
                            issues=("chain_dp requires a single linear component chain",))
    labels = [Label({}, 0, (), ())]
    explored = 0
    reasons = set()
    best = None
    best_cost = float("inf")
    for index, node in enumerate(order):
        frontiers = {}
        for label in labels:
            for option in problem.options[node]:
                if explored >= max_assignments:
                    return SearchResult(best, explored, False, limited=True)
                explored += 1
                assignment = {**label.assignment, node: option}
                try:
                    cost, demands, links, _ = problem.evaluate(assignment)
                except ValueError as exc:
                    reasons.add(str(exc))
                    continue
                resources = tuple(getattr(demands[host], field) for host in problem.nodes
                                  for field in demands[host].__dataclass_fields__) + tuple(links.values())
                # Different consumed Warm instances have different future reuse opportunities.
                reused = tuple(sorted(item.instance_id for item in problem.reused_instances(assignment).values()))
                candidate = Label(assignment, cost, resources, (option[0], option[1].output_bytes, reused))
                if index == len(order) - 1 and cost < best_cost:
                    best, best_cost = assignment, cost
                if candidate.boundary not in frontiers:
                    frontiers[candidate.boundary] = ParetoFrontier(len(resources))
                frontiers[candidate.boundary].add(candidate)
        labels = [label for frontier in frontiers.values() for label in frontier.values()]
        if not labels:
            break
    return SearchResult(best, explored, True,
                        issues=tuple(sorted(reasons)) if best is None else ())
