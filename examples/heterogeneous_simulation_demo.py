"""Run a real reference signal chain on modeled DSP/FPGA resource allocations."""
import argparse
import json
from pathlib import Path

import numpy as np

from components.runtime import DataPacket
from deployment.config import load_deployment_manager
from matching import match_task
from task_repr import parse_external_task, refine_business_task

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "examples/ResourceSimulations/heterogeneous_simulation.json"


def sample_task():
    request = json.loads((ROOT / "examples/BusinessRequests/External/07_spectrum_analysis.json")
                         .read_text(encoding="utf-8"))
    request["deadline_ms"] = request["arrival_ms"] + 1000
    task = refine_business_task(parse_external_task(request)).task
    return request, task, match_task(task)


def sample_frame(task):
    count, rate = 4096, 4_000_000
    samples = np.exp(2j * np.pi * 300_000 * np.arange(count) / rate)
    return {task.G.nodes[0].node_id: DataPacket(samples, rate, 100_000_000, 0, "time")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--algorithm', choices=('exact', 'chain_dp', 'heft'), default='exact')
    parser.add_argument('--frames', type=int, default=4)
    args = parser.parse_args()
    _, task, match = sample_task()
    manager = load_deployment_manager(CONFIG)
    before = manager.scheduler.placement_snapshot()
    result = manager.execute(task, match, [sample_frame(task) for _ in range(args.frames)],
                             algorithm=args.algorithm)
    restored = manager.scheduler.placement_snapshot()
    report = {"execution_mode": result.execution_mode,
              "execution_backend": "python_cpu_reference",
              "plan": result.plan.to_dict(), "frames_completed": len(result.frame_outputs),
              "output_items": {key: packet.item_count for key, packet in result.frame_outputs[-1].items()},
              "reservations_released": before["node_used"] == restored["node_used"] and
                                       before["link_used"] == restored["link_used"]}
    suffix = '' if args.algorithm == 'exact' else f'-{args.algorithm}'
    output = ROOT / f"outputs/heterogeneous-simulation{suffix}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
