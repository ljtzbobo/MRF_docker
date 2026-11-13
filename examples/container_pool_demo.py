"""Two synthetic spectrum tasks share three Warm reference instances."""
from dataclasses import replace
import json
from pathlib import Path

from deployment.simulation import load_simulation_manager
from examples.heterogeneous_simulation_demo import CONFIG, sample_task, sample_frame
from matching import match_task


def main():
    manager = load_simulation_manager(CONFIG)
    _, template, _ = sample_task()
    results = []
    try:
        for name in ('pool-task-a', 'pool-task-b'):
            task = replace(template, task_id=name)
            result = manager.execute(task, match_task(task), [sample_frame(task) for _ in range(4)], keep_warm=True)
            results.append({'task_id': name, 'plan': result.plan.to_dict(),
                            'completed_frames': len(result.frame_outputs), 'pool': manager.pool.snapshot()})
        target = Path(__file__).resolve().parents[1] / 'outputs' / 'container-pool-demo.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
        for result in results:
            print(result['task_id'], [(a['cid'], a['acquisition'], a['container_unit_id'])
                                     for a in result['plan']['allocations']])
        print(target)
    finally:
        manager.pool.close()


if __name__ == '__main__':
    main()
