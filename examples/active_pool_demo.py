"""Submit two overlapping synthetic tasks; report the real dispatch decision.

No processing sleeps or fake progress are used. Very short workloads can finish
before the follower arrives; the report then truthfully shows Warm/Cold instead.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
from threading import Event

from deployment.simulation import load_simulation_manager
from examples.heterogeneous_simulation_demo import CONFIG, sample_task, sample_frame
from matching import match_task


def main():
    manager=load_simulation_manager(CONFIG)
    _,template,_=sample_task()
    tasks=[replace(template,task_id=name,
                   X={**template.X,'remaining_deadline_ms':10000},
                   Q={**template.Q,'deadline_ms':template.X.get('observed_at_ms',0)+10000})
           for name in ('active-demo-a','active-demo-b')]
    matches=[match_task(task) for task in tasks]
    frames=[[sample_frame(task)]*128 for task in tasks]
    first_call=Event()
    def progress(event):
        if event['event']=='processing_node':first_call.set()
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first=executor.submit(manager.execute,tasks[0],matches[0],frames[0],keep_warm=True,progress=progress)
            if not first_call.wait(5):raise RuntimeError('first task did not start')
            second=executor.submit(manager.execute,tasks[1],matches[1],frames[1],keep_warm=True)
            results=[first.result(),second.result()]
        report={'input_origin':'synthetic reference IQ; not a dataset or hardware benchmark',
            'runs':[{'task_id':r.task_id,'completed_frames':len(r.frame_outputs),'plan':r.plan.to_dict()} for r in results],
            'pool':manager.pool.snapshot()}
        target=Path(__file__).resolve().parents[1]/'outputs/active-pool-demo.json'
        target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        for result in results:
            print(result.task_id,[(a.cid,a.acquisition,a.container_unit_id) for a in result.plan.allocations])
            print('actual wait ms:',result.plan.dispatch['actual_wait_ms'])
        print(target)
    finally:manager.pool.close()


if __name__=='__main__':main()
