"""Frame-boundary local rebinding for the software reference backend."""
from collections import deque
from dataclasses import replace
from math import isfinite
from time import monotonic, time

from resources.constraints import task_time_limit
from resources.measurements import PhysicalUsage, FPGA_FIELDS
from scheduler import SchedulingError


class DynamicScheduler:
    def __init__(self, manager):
        self.manager = manager
        self.lock = manager.scheduler._lock
        self.tasks = {}
        self.loads = {}
        self.history = deque(maxlen=256)
        self.threshold = .85

    def begin(self, task):
        with self.lock:
            if task.task_id in self.tasks:
                raise SchedulingError('task is already active or queued')
            limit = task_time_limit(task)
            self.tasks[task.task_id] = dict(start=monotonic(), limit=limit,
                priority=task.X.get('current_priority',0), events=deque(maxlen=32), risk_reported=False)
            self.emit('new_task', source_task_id=task.task_id)

    def end(self, task_id):
        with self.lock:
            self.tasks.pop(task_id,None)

    def emit(self, kind, **data):
        with self.lock:
            if kind not in ('new_task','priority_change','deadline_risk','resource_load'):
                raise ValueError('unknown dynamic event')
            allowed={'new_task':{'source_task_id'},'priority_change':{'task_id','priority'},
                     'deadline_risk':{'task_id','remaining_ms','predicted_ms'},
                     'resource_load':{'processing_node_id','utilization'}}
            if set(data)-allowed[kind]:raise ValueError('unknown event fields')
            if kind=='new_task' and (not isinstance(data.get('source_task_id'),str) or not data['source_task_id']):
                raise ValueError('source_task_id is required')
            target = data.get('task_id')
            if kind in ('priority_change','deadline_risk') and target not in self.tasks:
                raise SchedulingError('task is not active or queued')
            if kind=='deadline_risk':
                for key in ('remaining_ms','predicted_ms'):
                    if key in data and (type(data[key]) not in (int,float) or not isfinite(data[key])):
                        raise ValueError('deadline measurements must be finite numbers')
            if kind == 'priority_change':
                value = data.get('priority')
                if type(value) is not int or not 0 <= value <= 100:
                    raise ValueError('priority must be an integer in [0,100]')
                self.tasks[target]['priority'] = value
                # Reorder waiting claims only, never interrupt an executing task.
                pool = self.manager.pool
                if target in pool.pending:pool.pending[target].priority=value
                ordered = sorted(pool.pending.items(), key=lambda pair:
                    (-self.tasks.get(pair[0],{}).get('priority',0), pair[1].entered_at))
                pool.pending = dict(ordered)
                pool.condition.notify_all()
            if kind == 'resource_load':
                host, value = data.get('processing_node_id'), data.get('utilization')
                if host not in self.manager.scheduler.placement_planner.nodes:
                    raise ValueError('unknown processing node')
                if type(value) not in (int,float) or not isfinite(value) or not 0 <= value <= 1:
                    raise ValueError('utilization must be finite and in [0,1]')
                self.loads[host] = value
            event = dict(event=kind,at=time(),**data)
            self.history.append(event)
            for task_id, context in self.tasks.items():
                if kind == 'new_task' and task_id == data.get('source_task_id'): continue
                if target is None or target == task_id:
                    context['events'].append(event)
            return event

    def snapshot(self):
        with self.lock:
            return dict(backend='software_reference_frame_boundary', threshold=self.threshold,
                loads=dict(self.loads), active_tasks=[dict(task_id=k,priority=v['priority'],
                pending_events=len(v['events'])) for k,v in self.tasks.items()],events=list(self.history))

    def boundary(self, task, match, plan, nodes, remaining, algorithm, notify):
        pool, scheduler = self.manager.pool, self.manager.scheduler
        with self.lock:
            context = self.tasks[task.task_id]
            budget = None if context['limit'] is None else context['limit']-(monotonic()-context['start'])*1000
            predicted = remaining*(sum(a.runtime_p95_ms for a in plan.allocations)+sum(plan.edge_transfer_ms.values()))
            if budget is not None and predicted > budget and not context['risk_reported']:
                context['risk_reported'] = True
                self.emit('deadline_risk',task_id=task.task_id,remaining_ms=budget,predicted_ms=predicted)
            events = list(context['events']); context['events'].clear()
            if not events:
                overloaded = next((a.processing_node_id for a in plan.allocations
                    if self.loads.get(a.processing_node_id,0) >= self.threshold),None)
                if overloaded is not None:
                    events = [dict(event='resource_load',processing_node_id=overloaded,
                                   utilization=self.loads[overloaded],at=time(),source='persistent_telemetry')]
        if not events:return plan
        notify('dynamic_event',events=events)
        if not hasattr(self.manager.runtime,'rebind_many'):
            self._record(task.task_id,'unsupported_backend',events)
            notify('rebind_skipped',reason='runtime_state_transfer_unavailable')
            return plan
        with self.lock:
            bad = {host for host,value in self.loads.items() if value >= self.threshold}
            affected = [a for a in plan.allocations if a.processing_node_id in bad]
            if not affected:
                if all(e['event']=='resource_load' for e in events):
                    self._record(task.task_id,'unaffected',events)
                    return plan
                # Latency/priority/arrival: move at most one component, and only for an improvement.
                affected = sorted(plan.allocations,key=lambda a:a.runtime_p95_ms,reverse=True)[:1]
            for source in affected:
                budget = None if context['limit'] is None else context['limit']-(monotonic()-context['start'])*1000
                if budget is not None and budget <= 0: break
                state = scheduler._placement_state()
                used = {host:PhysicalUsage(**{key:max(0,getattr(value,key)-getattr(plan.node_demand[host],key))
                    for key in PhysicalUsage.__dataclass_fields__}) for host,value in state.node_used.items()}
                links = {key:max(0,value-plan.link_demand[key]) for key,value in state.link_used.items()}
                state = replace(state,node_used=used,link_used=links)
                candidates = []
                for host in scheduler.placement_planner.nodes:
                    if host == source.processing_node_id or host in bad:continue
                    fixed = {a.node_id:a.processing_node_id for a in plan.allocations}
                    fixed[source.node_id] = host
                    timed = replace(task,X={**task.X,'remaining_deadline_ms':max(0,budget) if budget is not None else None})
                    candidate = scheduler.placement_planner.plan(timed,match,state,fixed,
                        frame_count=remaining,algorithm=algorithm,max_assignments=10000)
                    if candidate.status != 'FEASIBLE_ESTIMATE':continue
                    # Preserve all unaffected implementations, not merely their hosts.
                    old = {a.node_id:a for a in plan.allocations}
                    if any(a.node_id != source.node_id and (a.implementation_id != old[a.node_id].implementation_id
                           or a.demand != old[a.node_id].demand) for a in candidate.allocations):continue
                    target = next(a for a in candidate.allocations if a.node_id==source.node_id)
                    old_profile = self.manager.runtime._profile(source,nodes[source.node_id]['parameters'])
                    new_profile = self.manager.runtime._profile(target,nodes[source.node_id]['parameters'])
                    if new_profile.input_bytes < old_profile.input_bytes or new_profile.output_bytes < old_profile.output_bytes:continue
                    migration = target.setup_p95_ms
                    cost = remaining*(sum(a.runtime_p95_ms for a in candidate.allocations)+sum(candidate.edge_transfer_ms.values()))+migration
                    current = remaining*(sum(a.runtime_p95_ms for a in plan.allocations)+sum(plan.edge_transfer_ms.values()))
                    if source.processing_node_id not in bad and cost >= current:continue
                    if context['limit'] is not None and cost > context['limit']-(monotonic()-context['start'])*1000:continue
                    candidates.append((cost,candidate,target))
                if not candidates:continue
                cost,candidate,target = min(candidates,key=lambda item:item[0])
                # In-process reference state is retained, not a Docker/CRIU checkpoint.
                allocations = tuple(replace(a,container_unit_id=old[a.node_id].container_unit_id,
                    lease_id=old[a.node_id].lease_id,runtime_identity=old[a.node_id].runtime_identity,
                    acquisition=old[a.node_id].acquisition) for a in candidate.allocations)
                updated = replace(plan,allocations=allocations,node_demand=candidate.node_demand,
                    link_demand=candidate.link_demand,edge_transfer_ms=candidate.edge_transfer_ms,
                    optimality_proven=False)
                before = scheduler.placement_instances[source.container_unit_id]
                item = pool._leased(source)
                if item.phase != 'READY':raise SchedulingError('rebinding requires a frame boundary')
                changed = next(a for a in allocations if a.node_id==source.node_id)
                undo = self.manager.runtime.rebind_many([(item.handle,changed,nodes[source.node_id])])
                try:
                    scheduler.placement_instances[source.container_unit_id] = replace(before,
                        implementation_id=changed.implementation_id,processor=changed.processor,
                        processing_node_id=changed.processing_node_id,memory_bytes=changed.demand.memory_bytes,
                        resident_demand=PhysicalUsage(memory_bytes=changed.demand.memory_bytes,
                            dsp_local_memory_bytes=changed.demand.dsp_local_memory_bytes,
                            **{key:getattr(changed.demand,key) for key in FPGA_FIELDS}))
                    scheduler.placement_active[task.task_id] = updated
                    item.allocation = changed
                    pool.batches[task.task_id]['plan'] = updated
                except BaseException:
                    self.manager.runtime.restore_bindings(undo)
                    scheduler.placement_instances[source.container_unit_id] = before
                    scheduler.placement_active[task.task_id] = plan
                    item.allocation = source
                    pool.batches[task.task_id]['plan'] = plan
                    raise
                record = self._record(task.task_id,'rebound',events,node_id=source.node_id,
                    source=source.processing_node_id,target=changed.processing_node_id,remaining_frames=remaining,
                    estimated_remaining_ms=cost,state_transfer='same_python_component_object')
                if updated.dispatch is not None:
                    updated.dispatch.setdefault('rebindings',[]).append(record)
                pool._save()
                pool.condition.notify_all()
                notify('rebound',plan=updated.to_dict(),node_id=source.node_id,source=source.processing_node_id,target=changed.processing_node_id)
                return updated  # At most one component per boundary; other bindings stay untouched.
        self._record(task.task_id,'no_feasible_local_change',events)
        notify('rebind_skipped',reason='no_feasible_local_change')
        return plan

    def _record(self,task_id,status,events,**details):
        with self.lock:
            record=dict(event='local_rebind',task_id=task_id,status=status,at=time(),
                        triggers=[e['event'] for e in events],**details)
            self.history.append(record)
            return record
