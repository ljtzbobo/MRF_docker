"""Reproducible synthetic placement comparisons; never hardware calibration data."""

import argparse
import csv
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import random
import time

from matching.matcher import ComponentBinding, StaticMatchResult
from resources.measurements import (ComponentMeasurement, MeasurementCatalog,
                                    PhysicalHardwareProfile, PhysicalUsage)
from resources.placement import (EdgeTransfer, NetworkLink, PlacementPlanner,
                                 PlacementState, ProcessingNode)
from task_repr.template.refinement import (SpecificationEdge, SpecificationGraph,
                                          SpecificationNode, SpecificationSemantics,
                                          SpecificationTask)


def make_scenario(config):
    rng = random.Random(config["seed"])
    count, hosts = config["nodes"], config["hosts"]
    ids = tuple(f"unit-{index:03d}" for index in range(count))
    pairs = list(zip(ids, ids[1:])) if config["shape"] == "chain" else [
        (ids[(index - 1) // 2], ids[index]) for index in range(1, count)]
    nodes = tuple(SpecificationNode(key, key, "synthetic", {"in": "iq/complex"},
                                    {"out": "iq/complex"}, {"workload": index}, key)
                  for index, key in enumerate(ids))
    graph = SpecificationGraph(nodes, tuple(SpecificationEdge(a, "out", b, "in") for a, b in pairs),
                               (), "simulation", "1.0", "component_dag", {}, (), (), ())
    task = SpecificationTask(config["name"], SpecificationSemantics("simulation", "simulation", "rx", {}),
                             {"deadline_ms": 1_000_000}, {}, graph, {"observed_at_ms": 0})
    bindings = {node.node_id: ComponentBinding(f"SIM-{index}", dict(node.parameters))
                for index, node in enumerate(nodes)}
    match = StaticMatchResult(task.task_id, "MATCHED", bindings, tuple(pairs), ())
    capacity = PhysicalUsage(max(300, count * 35), count * 4096, 100000, count, count)
    hardware = tuple(ProcessingNode(f"host-{host}", PhysicalHardwareProfile(
        f"board-{host}", capacity, ("synthetic-accelerator",))) for host in range(hosts))
    links = tuple(NetworkLink(a.node_id, b.node_id, 10000, 0.2)
                  for a in hardware for b in hardware if a != b)
    profiles = []
    for host, node in enumerate(hardware):
        processor = ("cpu", "dsp", "fpga")[host % 3]
        for key, binding in bindings.items():
            usage = PhysicalUsage(rng.randint(30, 90) if processor == "cpu" else 10,
                                  rng.randint(1, 4) * 1024, 100,
                                  int(processor == "dsp"), int(processor == "fpga"))
            profiles.append(ComponentMeasurement(
                binding.cid, f"{binding.cid}-{host}", processor, node.hardware.hardware_id,
                () if processor == "cpu" else ("synthetic-accelerator",), binding.parameters,
                usage, rng.randint(1, 15), rng.randint(0, 4), 4096, 4096, 100,
                "synthetic simulation; no hardware measurement", timing_scope="synthetic"))
    transfers = tuple(EdgeTransfer(a, b, 100, 1000) for a, b in pairs)
    planner = PlacementPlanner(hardware, links, MeasurementCatalog(tuple(profiles)), transfers)
    state = PlacementState({key: PhysicalUsage() for key in planner.nodes},
                            {key: 0.0 for key in planner.links})
    return task, match, planner, state


def run(configs, max_assignments):
    rows = []
    for config in configs:
        task, match, planner, state = make_scenario(config)
        baseline = None
        for algorithm in ("exact", "chain_dp", "heft"):
            started = time.perf_counter()
            plan = planner.plan(task, match, state, algorithm=algorithm,
                                frame_count=config["frames"], max_assignments=max_assignments)
            elapsed = (time.perf_counter() - started) * 1000
            if algorithm == "exact" and plan.optimality_proven:
                baseline = plan.estimated_completion_ms
            rows.append({"scenario": config["name"], "algorithm": algorithm,
                         "status": plan.status, "optimality_proven": plan.optimality_proven,
                         "estimated_completion_ms": plan.estimated_completion_ms,
                         "planning_ms": round(elapsed, 4), "explored": plan.explored,
                         "ratio_to_proven_exact": (plan.estimated_completion_ms / baseline
                             if baseline and plan.estimated_completion_ms is not None else None),
                         "node_demand": {key: asdict(value) for key, value in plan.node_demand.items()},
                         "issues": list(plan.issues)})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", type=Path, default=Path(__file__).with_name("scenarios.json"))
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("baseline.json"))
    parser.add_argument("--max-assignments", type=int, default=30000)
    args = parser.parse_args()
    configs = json.loads(args.scenarios.read_text(encoding="utf-8"))["scenarios"]
    rows = run(configs, args.max_assignments)
    report = {"simulation_only": True, "created_at_utc": datetime.now(timezone.utc).isoformat(),
              "python": platform.python_version(), "platform": platform.platform(),
              "max_assignments": args.max_assignments, "scenarios": configs, "results": rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    columns = [key for key in rows[0] if key not in ("node_demand", "issues")]
    with args.output.with_suffix(".csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(args.output)


if __name__ == "__main__":
    main()
