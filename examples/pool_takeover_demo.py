"""Real Docker pool takeover across separate Python processes, using synthetic IQ."""
import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys

from deployment.docker_runtime import DockerRuntime
from deployment.session import DeploymentManager
from examples.heterogeneous_simulation_demo import sample_task,sample_frame
from matching import match_task
from resources.measurements import PhysicalHardwareProfile,PhysicalUsage,ComponentMeasurement,MeasurementCatalog
from resources.placement import PlacementPlanner,ProcessingNode
from scheduler import TaskScheduler


def fixture(image):
    _,task,_=sample_task()
    graph=replace(task.G,nodes=task.G.nodes[:1],edges=(),boundaries=(
        dict(direction='source',business_node_id='main-02',node_id=task.G.nodes[0].node_id,format='iq/complex'),
        dict(direction='sink',business_node_id='main-02',node_id=task.G.nodes[0].node_id,format='iq/baseband')),
        covered_outputs=(),uncovered_outputs=())
    task=replace(task,G=graph,Q={**task.Q,'deadline_ms':600000},X={**task.X,'remaining_deadline_ms':600000})
    match=match_task(task)
    if match.status!='MATCHED':raise RuntimeError(str(match))
    binding=next(iter(match.bindings.values()))
    hardware=PhysicalHardwareProfile('takeover-cpu',PhysicalUsage(cpu_millicores=2000,memory_bytes=1024*2**20))
    profile=ComponentMeasurement(binding.cid,'takeover-cpu-ddc','cpu',hardware.hardware_id,(),binding.parameters,
        PhysicalUsage(cpu_millicores=1000,memory_bytes=256*2**20),1,1000,65536,32768,1,
        'synthetic admission profile for lifecycle integration; not performance calibration',timing_scope='synthetic')
    planner=PlacementPlanner((ProcessingNode('takeover-cpu',hardware),),(),MeasurementCatalog((profile,)),())
    manager=DeploymentManager(TaskScheduler(placement_planner=planner),DockerRuntime('takeover-cpu',{'C03':image}))
    return manager,task,match


def child(stage,image,folder):
    manager,task,match=fixture(image)
    pool=manager.pool
    pool.configure_checkpoints(folder/'checkpoints.sqlite')
    pool.configure_persistence(folder/'pool.sqlite')
    result={'stage':stage,'recovery':pool.recovery}
    if stage=='seed':
        executed=manager.execute(task,match,[sample_frame(task)],keep_warm=True)
        result['instance_id']=executed.plan.allocations[0].container_unit_id
    elif stage=='adopt':
        seed=json.loads((folder/'seed.json').read_text())
        if pool.recovery['adopted_warm']!=[seed['instance_id']]:raise RuntimeError('Warm adoption failed')
        executed=manager.execute(task,match,[sample_frame(task)],keep_warm=True)
        if executed.plan.allocations[0].acquisition!='warm_reuse':raise RuntimeError('adopted Warm was not reused')
        result['checkpoint']=pool.checkpoint(seed['instance_id'])['id']
        if manager.scheduler.placement_snapshot()['node_used']['takeover-cpu']['memory_bytes']:
            raise RuntimeError('confirmed suspended container still holds running budget')
    elif stage=='restore':
        executed=manager.execute(task,match,[sample_frame(task)],keep_warm=True)
        if executed.plan.allocations[0].acquisition!='restore':raise RuntimeError('pool did not dispatch native restore')
        result['acquisition']=executed.plan.allocations[0].acquisition
        task=replace(task,task_id='interrupted-active-task')
        plan=pool.reserve(task,match_task(task),frame_count=1)
        nodes={n['id']:n for n in match.execution_graph['nodes']}
        for a in plan.allocations:pool.start(a,nodes[a.node_id])
        result['active_plan']=plan.to_dict()
    else:
        if 'interrupted-active-task' not in pool.recovery['interrupted_tasks']:raise RuntimeError('interruption not recorded')
        if not pool.recovery['quarantined']:raise RuntimeError('Active not quarantined')
        if not manager.scheduler.placement_snapshot()['node_used']['takeover-cpu']['cpu_millicores']:
            raise RuntimeError('uncertain Active lost its resource reservation')
        pool.retry_cleanup('interrupted-active-task')
        pool.close()
        result['reservations_released']=not manager.scheduler.placement_snapshot()['active_tasks']
    (folder/(stage+'.json')).write_text(json.dumps(result,indent=2),encoding='utf-8')
    if stage in ('seed','restore'):
        # Deliberately skip Python cleanup; actual daemon objects and journal survive.
        os._exit(0)
    pool.shutdown()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',default='mrf-component:checkpoint-test')
    parser.add_argument('--folder',type=Path,default=Path('outputs/pool-takeover'))
    parser.add_argument('--stage',choices=('seed','adopt','restore','quarantine'))
    args=parser.parse_args();args.folder.mkdir(parents=True,exist_ok=True)
    if args.stage:return child(args.stage,args.image,args.folder)
    if (args.folder/'pool.sqlite').exists():parser.error('choose a fresh --folder to preserve previous evidence')
    rows=[]
    for stage in ('seed','adopt','restore','quarantine'):
        run=subprocess.run([sys.executable,'-m','examples.pool_takeover_demo','--stage',stage,'--image',args.image,'--folder',str(args.folder)],capture_output=True,text=True)
        if run.returncode:
            rows.append(dict(stage=stage,status='failed',error=run.stderr));break
        rows.append(dict(stage=stage,status='ok',details=json.loads((args.folder/(stage+'.json')).read_text())))
    report=dict(status='complete' if len(rows)==4 and all(r['status']=='ok' for r in rows) else 'incomplete',stages=rows)
    (args.folder/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
