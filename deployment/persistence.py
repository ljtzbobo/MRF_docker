"""Atomic pool journal and a process-lifetime exclusive owner lock.

Docker instances are reconciled with the daemon, never trusted from JSON alone.
In-memory reference objects cannot survive a process exit and are not fabricated.
"""
from dataclasses import asdict, replace
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4
from time import monotonic

from resources.measurements import PhysicalUsage
from resources.placement import PlacementAllocation, PlacementPlan
from scheduler.manager import PlacementInstanceRecord
from scheduler import SchedulingError


def allocation_from(raw):
    return PlacementAllocation(**{**raw,'demand':PhysicalUsage(**raw['demand'])})


def plan_from(raw):
    values={k:v for k,v in raw.items() if k in PlacementPlan.__dataclass_fields__}
    values['allocations']=tuple(allocation_from(a) for a in raw['allocations'])
    values['node_demand']={k:PhysicalUsage(**v) for k,v in raw['node_demand'].items()}
    for key in ('link_demand','edge_transfer_ms'):
        values[key]={tuple(k.split('->',1)):v for k,v in raw[key].items()}
    values['issues']=tuple(raw['issues'])
    return PlacementPlan(**values)


class PoolJournal:
    def __init__(self,pool,path):
        self.pool=pool
        self.path=Path(path).resolve()
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.owner=open(str(self.path)+'.lock','a+b')
        self.owner.seek(0,2)
        if self.owner.tell()==0:self.owner.write(b'1');self.owner.flush()
        self.owner.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.owner.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.owner.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.owner.close()
            raise SchedulingError('another backend owns this pool journal')
        try:
            with sqlite3.connect(self.path) as db:
                db.execute('CREATE TABLE IF NOT EXISTS pool_state (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)')
            planner=pool.scheduler.placement_planner
            baseline=pool.scheduler.placement_baseline
            config=dict(nodes=[asdict(n) for n in planner.nodes.values()],links=[asdict(n) for n in planner.links.values()],
                baseline=None if baseline is None else dict(node_used={k:asdict(v) for k,v in baseline.node_used.items()},
                    link_used=sorted([[*key,value] for key,value in baseline.link_used.items()])))
            self.configuration=sha256(json.dumps(config,sort_keys=True).encode()).hexdigest()
            self.engine_id=None
            if hasattr(pool.runtime,'_docker'):
                self.engine_id=pool.runtime._docker('info','--format','{{.ID}}')
                if not self.engine_id:raise SchedulingError('cannot reconcile without Docker daemon identity')
        except BaseException:
            self.close();raise

    def read(self):
        with sqlite3.connect(self.path) as db:
            row=db.execute('SELECT body FROM pool_state WHERE id=1').fetchone()
        if row is None:return None
        data=json.loads(row[0])
        if data.get('version')!=1 or data['configuration']!=self.configuration or data['engine_id']!=self.engine_id:
            raise SchedulingError('pool journal version, hardware configuration or Docker daemon differs')
        return data

    def save(self):
        pool=self.pool
        with pool.lock:
            instances=[]
            for r in pool.instances.values():
                raw=asdict(r)
                raw['handle']=asdict(r.handle) if hasattr(r.handle,'container_id') else None
                instances.append(raw)
            with pool.costs.lock:
                costs=[dict(key=list(key),action=action,samples=list(samples))
                       for (key,action),samples in pool.costs.values.items()]
            body=dict(version=1,configuration=self.configuration,engine_id=self.engine_id,instances=instances,
                plans=[p.to_dict() for p in pool.scheduler.placement_active.values()],
                queued=[r.task_id for r in pool.pending.values()],recovery=pool.recovery,
                costs=costs)
            with sqlite3.connect(self.path) as db:
                db.execute('INSERT OR REPLACE INTO pool_state VALUES (1,?)',(json.dumps(body),))

    def recover(self):
        from .pool import PoolInstance
        data=self.read()
        if data is None:return
        pool=self.pool
        pool.recovery=dict(previous_queued=data.get('queued',[]),interrupted_tasks=[p['task_id'] for p in data['plans']],
                           adopted_warm=[],missing=[],quarantined=[],in_memory_lost=[])
        for raw in data['instances']:
            a=allocation_from(raw['allocation'])
            raw={**raw,'allocation':a,'handle':None}
            item=PoolInstance(**raw)
            pool.instances[item.instance_id]=item
            if item.state=='Release':continue
            if not hasattr(pool.runtime,'reconcile_handle'):
                item.state,item.phase,item.task_id,item.lease_id='Release','LOST_ON_RESTART',None,None
                pool.recovery['in_memory_lost'].append(item.instance_id)
                continue
            handle,running=pool.runtime.reconcile_handle(a)
            item.uncertain_start=False
            item.last_used=monotonic()
            if handle is None:
                item.handle=None
                if item.state=='Warm':item.state,item.phase='Release','MISSING'
                else:item.phase='QUARANTINED'
                pool.recovery['missing'].append(item.instance_id)
            else:
                item.handle=handle
                if item.state=='Warm' and running:
                    try:
                        pool.runtime.health(handle);pool.runtime.reset(handle,a,item.node)
                        item.phase='READY'
                        pool.recovery['adopted_warm'].append(item.instance_id)
                    except Exception as exc:
                        item.phase,item.error='QUARANTINED',str(exc)
                else:item.phase='QUARANTINED'
            # A fresh lease generation rejects every pre-restart caller.
            if item.state=='Active':
                item.lease_id=uuid4().hex
                item.allocation=replace(item.allocation,lease_id=item.lease_id)
            if item.phase=='QUARANTINED':pool.recovery['quarantined'].append(item.instance_id)
            if item.state not in ('Active','Warm'):continue
            demand=a.demand
            resident=PhysicalUsage(memory_bytes=demand.memory_bytes,dsp_local_memory_bytes=demand.dsp_local_memory_bytes,
                **{k:getattr(demand,k) for k in ('fpga_lut','fpga_ff','fpga_dsp48','fpga_bram36')})
            if item.state=='Warm' and item.phase=='QUARANTINED':resident=demand
            pool.scheduler.placement_instances[item.instance_id]=PlacementInstanceRecord(item.instance_id,a.cid,
                a.implementation_id,a.processor,a.processing_node_id,demand.memory_bytes,
                'ACTIVE' if item.state=='Active' else 'WARM',item.task_id,resident)
        if hasattr(pool.runtime,'reconcile_handle'):
            for raw in data['plans']:
                plan=plan_from(raw)
                plan=replace(plan,allocations=tuple(pool.instances[a.container_unit_id].allocation for a in plan.allocations))
                pool.scheduler.placement_active[plan.task_id]=plan
                pool.batches[plan.task_id]=dict(plan=plan,completed_frames=0,admitted_at=monotonic())
        for row in data.get('costs',[]):
            for value in row['samples']:pool.costs.observe(tuple(row['key']),row['action'],value)
        self.save()

    def close(self):
        if not self.owner.closed:self.owner.close()
