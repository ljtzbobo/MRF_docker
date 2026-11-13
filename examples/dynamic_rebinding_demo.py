"""Deterministic event injection after frame one; no artificial processing delay."""
import json
from pathlib import Path
from deployment.simulation import load_simulation_manager
from examples.heterogeneous_simulation_demo import CONFIG,sample_task,sample_frame

def main():
    manager=load_simulation_manager(CONFIG)
    _,task,match=sample_task()
    events=[]
    def progress(event):
        events.append(event)
        if event['event']=='frame_completed' and event['frame_index']==1:
            plan=manager.scheduler.placement_active[task.task_id]
            manager.dynamic.emit('priority_change',task_id=task.task_id,priority=80)
            manager.dynamic.emit('new_task',source_task_id='incoming-synthetic-task')
            manager.dynamic.emit('deadline_risk',task_id=task.task_id)
            manager.dynamic.emit('resource_load',processing_node_id=plan.allocations[-1].processing_node_id,utilization=.95)
    try:
        result=manager.execute(task,match,[sample_frame(task)]*4,keep_warm=True,progress=progress)
        report=dict(input_origin='synthetic software reference; not hardware migration',
            frames=len(result.frame_outputs),plan=result.plan.to_dict(),dynamic=manager.dynamic.snapshot())
        target=Path(__file__).resolve().parents[1]/'outputs/dynamic-rebinding-demo.json'
        target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report['plan']['dispatch'].get('rebindings',[]),ensure_ascii=False))
        print(target)
    finally:manager.pool.close()

if __name__=='__main__':main()
