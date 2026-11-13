"""Common task deadline semantics for normalized and placement planning."""
import math

from task_repr.template.refinement import SpecificationTask


def task_time_limit(task):
    if isinstance(task, SpecificationTask):
        remaining = task.X.get("remaining_deadline_ms")
        deadline = task.Q.get("deadline_ms")
        observed = task.X.get("observed_at_ms")
        limits = [remaining] if remaining is not None else []
        if deadline is not None and observed is not None:
            limits.append(deadline - observed)
    else:
        limits = [task.X.remaining_deadline_ms, task.Q.max_latency_ms,
                  task.Q.max_response_ms]
    for value in limits:
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
            raise ValueError("task time limit must be finite")
    return min((value for value in limits if value is not None), default=None)
