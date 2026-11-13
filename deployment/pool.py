"""Single-backend component pool. The scheduler remains the resource ledger.

Cold is an image/template, Active/Warm/Release are instance lifecycle states.
Operational phases and exclusive lease tokens refine those states. Blocking
runtime calls run outside the scheduler lock; resources stay reserved until
reset or confirmed destruction finishes.
"""
from copy import deepcopy
from dataclasses import dataclass, replace
from threading import Condition
from time import monotonic
from uuid import uuid4

from resources.placement import ReusableInstance
from resources.measurements import PhysicalUsage
from resources.constraints import task_time_limit
from scheduler import SchedulingError
from .docker_runtime import DockerCleanupError
from .action_costs import ActionCosts


@dataclass
class PoolInstance:
    instance_id: str
    allocation: object
    node: dict
    runtime_identity: str
    state: str = "Active"
    phase: str = "CREATING"
    handle: object = None
    task_id: str | None = None
    lease_id: str | None = None
    last_used: float = 0
    reuse_count: int = 0
    error: str | None = None
    uncertain_start: bool = False
    last_acquisition_ms: float | None = None


@dataclass
class PendingRequest:
    task_id: str
    owner: str
    plan: object
    targets: dict
    entered_at: float
    wait_until: float
    priority: int = 0


class GlobalContainerPool:
    def __init__(self, scheduler, runtime, *, max_warm=8, idle_timeout_s=60):
        if type(max_warm) is not int or max_warm < 0 or idle_timeout_s <= 0:
            raise ValueError("invalid pool retention policy")
        self.scheduler, self.runtime = scheduler, runtime
        self.lock = scheduler._lock
        self.instances = {}
        self.max_warm = max_warm
        self.idle_timeout_s = idle_timeout_s
        self.condition = Condition(self.lock)
        self.pending = {}
        self.batches = {}
        self.costs = ActionCosts()
        self.active_reuse_enabled = True
        self.max_pending = 16
        self.max_queue_wait_s = 5.0
        self.max_action_candidates = 16
        self.checkpoints = None
        self.journal = None
        self.recovery = {}
        self.persistence_error = None
        self.closed = False
        self._shutdown_in_progress = False

    def configure_persistence(self,path):
        from .persistence import PoolJournal
        with self.lock:
            if self.instances or self.batches or self.pending:
                raise SchedulingError('persistence must be configured before accepting tasks')
            journal=PoolJournal(self,path)
            self.journal=journal
            try:journal.recover();journal.save()
            except BaseException:
                journal.close();self.journal=None
                raise

    def _save(self):
        if self.journal is not None:
            try:self.journal.save()
            except Exception as exc:
                self.persistence_error=str(exc)
                raise SchedulingError('pool journal write failed; new admissions disabled: '+str(exc)) from exc

    def shutdown(self):
        with self.lock:
            if self.closed:return
            if self.pending:raise SchedulingError('cannot shut down while tasks are waiting')
            self.closed=True
            self._shutdown_in_progress=True
        try:
            if self.journal is None or not hasattr(self.runtime,'reconcile_handle'):self.close()
            self._save()
        finally:
            if self.journal is not None:self.journal.close()
            self._shutdown_in_progress=False

    def configure_checkpoints(self,path):
        from .checkpoints import DockerCheckpoints
        from .docker_runtime import DockerRuntime
        if not isinstance(self.runtime,DockerRuntime):
            raise SchedulingError('native checkpoints require DockerRuntime, not software simulation')
        self.checkpoints = DockerCheckpoints(self.runtime,path)

    def checkpoint(self,instance_id):
        if self.closed:raise SchedulingError('pool is closed')
        if self.checkpoints is None:raise SchedulingError('checkpoint store is not configured')
        with self.lock:
            item=self.instances.get(instance_id)
            if item is None or item.state!='Warm' or item.phase!='READY' or self._head(instance_id) is not None:
                raise SchedulingError('only an unclaimed idle Warm instance can be checkpointed')
            item.phase='CHECKPOINTING'
            self._save()
        try:
            manifest=self.checkpoints.save(item.handle,item.allocation,item.node)
        except Exception as exc:
            with self.lock:
                self._quarantine_warm(item,exc)
                self._save()
            raise
        with self.condition:
            self.scheduler.evict_placement_warm(instance_id)
            item.state,item.phase,item.handle='Release','SUSPENDED',None
            self._save()
            self.condition.notify_all()
        return manifest

    def _restore_candidates(self,nodes,identities):
        if self.checkpoints is None:return {}
        result={}
        for node_id,node in nodes.items():
            result[node_id]=tuple(ReusableInstance(row['unit_id'],row['configuration']['processing_node_id'],
                row['configuration']['cid'],row['configuration']['implementation_id'],
                PhysicalUsage(**row['configuration']['demand']),PhysicalUsage(),'restore',
                *self.costs.estimate(self._cost_key(identities[node['cid']],row['configuration']['implementation_id'],
                    row['configuration']['processing_node_id'],node),'restore',row['allocation']['setup_p95_ms']))
                for row in self.checkpoints.compatible(node,identities[node['cid']]))
        return result

    @staticmethod
    def configuration(node):
        return {key: node[key] for key in ("cid", "parameters", "inputs", "outputs")}

    def _head(self, identifier):
        return next((r.task_id for r in self.pending.values() if identifier in r.targets.values()), None)

    def _cost_key(self, identity, implementation, host, node):
        return self.costs.key(identity, implementation, host, self.configuration(node))

    def _candidates(self, nodes, identities, task_id=None, future_owner=None):
        candidates = {}
        for node_id, node in nodes.items():
            compatible = []
            for item in self.instances.values():
                warm = item.state == "Warm" and item.phase == "READY"
                active = (future_owner is not None and item.state == "Active" and item.task_id == future_owner
                          and item.phase in ("READY", "RUNNING") and item.handle is not None)
                if (not (warm or active) or (warm and self._head(item.instance_id) not in (None, task_id))
                        or item.runtime_identity != identities[node['cid']]
                        or self.configuration(item.node) != self.configuration(node)):
                    continue
                a = item.allocation
                action = 'active_reuse' if active else 'warm_reuse'
                key = self._cost_key(item.runtime_identity, a.implementation_id, a.processing_node_id, node)
                cost, basis = self.costs.estimate(key, action, a.setup_p95_ms)
                compatible.append(ReusableInstance(item.instance_id, a.processing_node_id, a.cid,
                    a.implementation_id, a.demand, self.scheduler.placement_instances[item.instance_id].resident_demand,
                    action, cost, basis))
            candidates[node_id] = tuple(compatible)
        return candidates

    def _cold_costs(self, nodes, identities):
        result = {}
        planner = self.scheduler.placement_planner
        for node_id, node in nodes.items():
            result[node_id] = {}
            for host, resource in planner.nodes.items():
                for profile in planner.measurements.candidates(node['cid'], node['parameters'], resource.hardware):
                    key = self._cost_key(identities[node['cid']], profile.implementation_id, host, node)
                    result[node_id][host, profile.implementation_id] = self.costs.estimate(
                        key, 'cold_start', profile.effective_setup_ms)
        return result

    def _future_state(self, owner):
        state = self.scheduler._placement_state()
        plan = self.scheduler.placement_active[owner]
        used, links = dict(state.node_used), dict(state.link_used)
        for host, demand in plan.node_demand.items():
            used[host] = PhysicalUsage(**{key: max(0, getattr(used[host], key)-getattr(demand, key))
                                         for key in PhysicalUsage.__dataclass_fields__})
        for a in plan.allocations:
            resident = self.scheduler.placement_instances[a.container_unit_id].resident_demand
            host = a.processing_node_id
            used[host] = PhysicalUsage(**{key: getattr(used[host], key)+getattr(resident,key)
                                         for key in PhysicalUsage.__dataclass_fields__})
        for pair, demand in plan.link_demand.items():
            links[pair] = max(0, links[pair]-demand)
        return replace(state, node_used=used, link_used=links)

    def _remaining_ms(self, owner):
        batch = self.batches[owner]
        plan = batch['plan']
        frames = max(0, plan.frame_count-batch['completed_frames'])
        process = sum(a.runtime_p95_ms for a in plan.allocations)+sum(plan.edge_transfer_ms.values())
        # Count the whole current frame and return preparation: deliberately conservative.
        return frames*process + sum(a.estimated_acquisition_ms or a.setup_p95_ms for a in plan.allocations)

    def _evaluate_actions(self, task, match, nodes, identities, fixed_nodes, options, *, allow_active=True):
        evaluated_at = monotonic()
        planner = self.scheduler.placement_planner
        state = self.scheduler._placement_state()
        cold_costs = self._cold_costs(nodes, identities)
        decisions, choices = [], []
        attempts = 0
        remaining_search = options.get('max_assignments',100000)
        if type(remaining_search) is not int or remaining_search < 1:
            raise ValueError('max_assignments must be positive')

        def consider(action, candidate_state, required=None, owner=None):
            nonlocal attempts, remaining_search
            if action != 'cold_start' and remaining_search <= 1:
                return  # Keep a final search allowance for the cold fallback.
            attempts += 1
            allowance = remaining_search if action == 'cold_start' else max(1,
                remaining_search // max(2,self.max_action_candidates+2-attempts))
            plan = planner.plan(task, match, replace(candidate_state, required_reuse=required,
                                cold_setup_costs=cold_costs), fixed_nodes,
                                **{**options,'max_assignments':allowance})
            remaining_search -= plan.explored
            wait = 0.0
            targets = {a.node_id:a.container_unit_id for a in plan.allocations if a.acquisition != 'cold_start'}
            if owner is not None:
                ids = set(targets.values())
                wait = self._remaining_ms(owner) + sum(r.plan.estimated_completion_ms or 0
                    for r in self.pending.values() if ids.intersection(r.targets.values()))
            total = None if plan.estimated_completion_ms is None else wait+plan.estimated_completion_ms
            limit = task_time_limit(task)
            feasible = (plan.status == 'FEASIBLE_ESTIMATE' and total is not None and limit is not None and total <= limit
                        and (action != 'active_reuse' or (len(self.pending) < self.max_pending
                             and wait <= self.max_queue_wait_s*1000)))
            reason = ('within_deadline_and_capacity' if feasible else
                      plan.status if plan.status != 'FEASIBLE_ESTIMATE' else
                      'queue_and_execution_exceed_deadline' if limit is None or total > limit else
                      'queue_full' if action == 'active_reuse' and len(self.pending) >= self.max_pending else
                      'queue_wait_limit_exceeded' if action == 'active_reuse' and wait > self.max_queue_wait_s*1000 else
                      'no_feasible_dispatch')
            decisions.append(dict(action=action, owner_task_id=owner, feasible=feasible, reason=reason,
                estimated_wait_ms=wait, estimated_execution_ms=plan.estimated_completion_ms,
                estimated_total_ms=total, additional_preparation_ms=sum(a.estimated_acquisition_ms or 0 for a in plan.allocations),
                capacity_blocked=any('capacity exceeded' in issue for issue in plan.issues)))
            if feasible:
                choices.append(({'active_reuse':0,'warm_reuse':1,'restore':2,'cold_start':3}[action],total,
                                -len(targets), plan, owner, targets, len(decisions)-1))

        if allow_active and self.active_reuse_enabled and len(self.pending) < self.max_pending:
            owners = dict.fromkeys(r.task_id for r in self.instances.values()
                if r.state == 'Active' and r.phase in ('READY','RUNNING') and r.handle is not None and r.task_id in self.batches)
            for owner in owners:
                candidates = self._candidates(nodes, identities, task.task_id, owner)
                future = replace(self._future_state(owner), reuse_candidates=candidates)
                for node_id, items in candidates.items():
                    for item in items:
                        if item.acquisition == 'active_reuse' and attempts < self.max_action_candidates//2:
                            consider('active_reuse',future,{node_id:item.instance_id},owner)
        elif allow_active and self.active_reuse_enabled:
            decisions.append(dict(action='active_reuse',feasible=False,reason='queue_full'))
        warm = self._candidates(nodes, identities, task.task_id)
        for node_id, items in warm.items():
            for item in items:
                if attempts < self.max_action_candidates-2:
                    consider('warm_reuse',replace(state,reuse_candidates=warm),{node_id:item.instance_id})
        restores=self._restore_candidates(nodes,identities)
        mixed={key:tuple(warm.get(key,()))+tuple(restores.get(key,())) for key in nodes}
        for node_id,items in restores.items():
            for item in items:
                if attempts < self.max_action_candidates:
                    consider('restore',replace(state,reuse_candidates=mixed),{node_id:item.instance_id})
        consider('cold_start',replace(state,reuse_candidates=None))
        if not any(restores.values()):
            decisions.append(dict(action='restore',feasible=False,reason='no_compatible_checkpoint'))
        limit = task_time_limit(task)
        remaining = None if limit is None else max(0,limit-(monotonic()-evaluated_at)*1000)
        still_feasible = []
        for choice in choices:
            if remaining is None or choice[1] > remaining:
                decisions[choice[-1]]['feasible'] = False
                decisions[choice[-1]]['reason'] = 'decision_time_exhausts_deadline'
            else:
                still_feasible.append(choice)
        choices = still_feasible
        if not choices:
            return None, decisions
        _,_,_,plan,owner,targets,index = min(choices,key=lambda c:c[:3])
        decisions[index]['selected'] = True
        return (replace(plan,time_limit_ms=remaining),owner,targets), decisions

    def preview(self, task, match, fixed_nodes=None, **options):
        nodes = {node['id']:node for node in match.execution_graph['nodes']}
        identities = {node['cid']:self.runtime.pool_identity(node['cid']) for node in nodes.values()}
        with self.lock:
            self._check_task(task.task_id)
            choice,decisions = self._evaluate_actions(task,match,nodes,identities,fixed_nodes,options)
            if choice is None:
                state = replace(self.scheduler._placement_state(),cold_setup_costs=self._cold_costs(nodes,identities))
                plan = self.scheduler.placement_planner.plan(task,match,state,fixed_nodes,**options)
                if plan.status == 'FEASIBLE_ESTIMATE':
                    plan = replace(plan,status='SEARCH_LIMIT',optimality_proven=False,
                                   issues=plan.issues+('dispatch candidate search did not prove a feasible action',))
                return self._dispatch_plan(plan,decisions,None,0)
            plan,owner,_ = choice
            return self._dispatch_plan(plan,decisions,owner,0)

    def _check_task(self, task_id):
        if self.closed or self.persistence_error:
            raise SchedulingError('pool is closed or persistence is unavailable')
        if task_id in self.scheduler.placement_active or task_id in self.scheduler.active or task_id in self.pending:
            raise SchedulingError(f'task {task_id} is already active or queued')

    @staticmethod
    def _dispatch_plan(plan, decisions, owner, waited):
        return replace(plan, optimality_proven=False, dispatch=dict(
            policy='active_then_warm_then_restore_then_cold_with_deadline_guard', queue_mode='whole_task_fifo',
            search_scope='bounded_reuse_anchors_then_module3_placement', owner_task_id=owner,
            estimated_wait_ms=next((d['estimated_wait_ms'] for d in decisions if d.get('selected')),0)
                              if owner else 0,
            actual_wait_ms=waited, reservation_status='preview', decisions=decisions,
            cost_note='profile processing plus observed preparation maxima or profile fallback; no task-p95 guarantee',
            actual_preparation_ms={}))

    def _commit(self, plan, nodes, identities):
        reused = {a.container_unit_id for a in plan.allocations if a.acquisition in ('warm_reuse','active_reuse')}
        allocations = tuple(replace(a,
            container_unit_id=a.container_unit_id if a.acquisition != 'cold_start' else 'pool-'+uuid4().hex,
            lease_id=uuid4().hex, runtime_identity=identities[a.cid]) for a in plan.allocations)
        plan = replace(plan,allocations=allocations)
        claimed=[]
        try:
            for a in allocations:
                if a.acquisition=='restore':
                    self.checkpoints.claim(a.container_unit_id,a.lease_id)
                    claimed.append(a)
            self.scheduler.reserve_pool_plan(plan,reused)
        except BaseException:
            for a in claimed:self.checkpoints.unclaim(a.container_unit_id,a.lease_id)
            raise
        for a in allocations:
            item = self.instances.get(a.container_unit_id)
            if item is None:
                item = PoolInstance(a.container_unit_id,a,deepcopy(nodes[a.node_id]),identities[a.cid])
                self.instances[item.instance_id] = item
            else:
                item.reuse_count += 1
            item.allocation,item.node = a,deepcopy(nodes[a.node_id])
            item.state,item.phase = 'Active','LEASED'
            item.task_id,item.lease_id,item.error = plan.task_id,a.lease_id,None
        self.batches[plan.task_id] = dict(plan=plan,completed_frames=0,admitted_at=monotonic())
        if plan.dispatch is not None:
            plan.dispatch['reservation_status'] = 'reserved'
        self._save()
        return plan

    def reserve(self, task, match, fixed_nodes=None, *, notify=lambda *args, **kwargs:None, **options):
        entered = monotonic()
        limit = task_time_limit(task)
        deadline = None if limit is None else entered+limit/1000
        nodes = {node["id"]: node for node in match.execution_graph["nodes"]}
        # Resolve image identity before taking the shared lock (Docker may block).
        identities = {node["cid"]: self.runtime.pool_identity(node["cid"]) for node in nodes.values()}
        self.reap_expired()
        allow_active, waited, fallback = True, 0.0, None
        attempts_history = []
        while True:
            with self.lock:
                self._check_task(task.task_id)
                if deadline is not None and monotonic() >= deadline:
                    raise SchedulingError('task deadline exhausted before instance dispatch')
                timed_task = task if deadline is None else replace(task,X={**task.X,
                    'remaining_deadline_ms':max(0,(deadline-monotonic())*1000)})
                choice, decisions = self._evaluate_actions(timed_task,match,nodes,identities,fixed_nodes,options,allow_active=allow_active)
                if choice is not None:
                    plan,owner,targets = choice
                    if fallback:
                        decisions = attempts_history + decisions + [dict(action='queue_fallback',feasible=False,
                            reason=fallback,actual_wait_ms=waited)]
                    plan = self._dispatch_plan(plan,decisions,owner,waited)
                    if owner is None:
                        return self._commit(plan,nodes,identities)
                    wait_until = min(deadline-(plan.estimated_completion_ms or 0)/1000,
                                     monotonic()+self.max_queue_wait_s)
                    request = PendingRequest(task.task_id,owner,plan,targets,monotonic(),wait_until,
                                             priority=task.X.get('current_priority',0))
                    self.pending[task.task_id] = request
                    self.pending = dict(sorted(self.pending.items(),key=lambda pair:(-pair[1].priority,pair[1].entered_at)))
                    self._save()
                else:
                    request = None
                # Make room by confirmed destruction of idle instances, never Active ones.
                warm = sorted((r for r in self.instances.values() if r.state == "Warm" and r.phase == "READY" and self._head(r.instance_id) is None),
                              key=lambda r: r.last_used)
                victim = warm[0].instance_id if warm and any(d.get('capacity_blocked') and
                    d.get('reason') in ('INFEASIBLE','HEURISTIC_FAILED') for d in decisions) else None
            if request is None:
                if victim is None:
                    raise SchedulingError('no dispatch action meets remaining deadline and capacity: '+str(decisions))
                try:
                    self.evict(victim)
                except SchedulingError:
                    pass
                continue
            try:
                notify('waiting_for_instance',plan=plan.to_dict(),total_frames=plan.frame_count,
                       estimated_wait_ms=plan.dispatch['estimated_wait_ms'],owner_task_id=owner)
                while True:
                    with self.condition:
                        remaining = request.wait_until-monotonic()
                        records = [self.instances.get(identifier) for identifier in targets.values()]
                        if any(r is None or r.state == 'Release' or r.phase == 'QUARANTINED' for r in records):
                            fallback = 'owner_failed_or_instance_unavailable'
                            break
                        if remaining <= 0:
                            fallback = 'queue_budget_exhausted'
                            break
                        if all(r.state == 'Warm' and r.phase == 'READY' and self._head(r.instance_id)==task.task_id for r in records):
                            waited = (monotonic()-request.entered_at)*1000
                            timed_task = replace(task,X={**task.X,'remaining_deadline_ms':max(0,(deadline-monotonic())*1000)})
                            state = replace(self.scheduler._placement_state(),
                                reuse_candidates=self._candidates(nodes,identities,task.task_id), required_reuse=targets,
                                cold_setup_costs=self._cold_costs(nodes,identities))
                            ready = self.scheduler.placement_planner.plan(timed_task,match,state,fixed_nodes,**options)
                            if (ready.status != 'FEASIBLE_ESTIMATE' or
                                    ready.estimated_completion_ms > (deadline-monotonic())*1000):
                                fallback = 'capacity_or_deadline_changed'
                                break
                            active_ids = {a.container_unit_id for a in request.plan.allocations if a.acquisition=='active_reuse'}
                            ready = replace(ready,allocations=tuple(replace(a,acquisition='active_reuse')
                                if a.container_unit_id in active_ids else a for a in ready.allocations))
                            ready = self._dispatch_plan(ready,decisions,owner,waited)
                            return self._commit(ready,nodes,identities)
                        self.condition.wait(timeout=min(remaining,.25))
            finally:
                with self.condition:
                    self.pending.pop(task.task_id,None)
                    self._save()
                    self.condition.notify_all()
            waited = (monotonic()-request.entered_at)*1000
            attempts_history = [{**d,'initially_selected':d.get('selected',False),'selected':False} for d in decisions]
            allow_active = False
            notify('queue_fallback',reason=fallback,actual_wait_ms=waited)

    def _leased(self, allocation):
        if self.closed:raise SchedulingError('pool is closed')
        item = self.instances.get(allocation.container_unit_id)
        if item is None or item.state != "Active" or item.lease_id != allocation.lease_id:
            raise SchedulingError("stale or invalid pool lease")
        return item

    def start(self, allocation, node):
        started = monotonic()
        with self.lock:
            item = self._leased(allocation)
            item.phase = "CREATING" if item.handle is None else "CHECKING"
        try:
            if item.handle is None:
                if allocation.acquisition=='restore':
                    try:handle=self.checkpoints.restore(allocation,node)
                    except DockerCleanupError:raise
                    except Exception as restore_error:
                        batch=self.batches[item.task_id]
                        limit=batch['plan'].time_limit_ms
                        elapsed=(monotonic()-batch['admitted_at'])*1000
                        key=self._cost_key(item.runtime_identity,allocation.implementation_id,allocation.processing_node_id,node)
                        cold_cost,_=self.costs.estimate(key,'cold_start',allocation.setup_p95_ms)
                        # Do not silently spend an expired restore budget on a cold fallback.
                        outstanding=sum(a.runtime_p95_ms*batch['plan'].frame_count for a in batch['plan'].allocations)
                        outstanding+=sum(batch['plan'].edge_transfer_ms.values())*batch['plan'].frame_count
                        outstanding+=sum((a.estimated_acquisition_ms or a.setup_p95_ms) for a in batch['plan'].allocations
                            if a.node_id!=allocation.node_id and self.instances[a.container_unit_id].phase not in ('READY','RUNNING'))
                        if limit is not None and elapsed+cold_cost+outstanding>limit:
                            raise SchedulingError('restore failed; insufficient remaining time for cold fallback') from restore_error
                        try:
                            source,running=self.runtime.reconcile_handle(allocation)
                            if running:raise DockerCleanupError('restore fallback found a running source; reconciliation required')
                            if source is not None:self.runtime.stop(source)
                        except Exception as cleanup:
                            raise DockerCleanupError('restore fallback source cleanup is unconfirmed: '+str(cleanup)) from restore_error
                        allocation=replace(allocation,acquisition='cold_start')
                        item.allocation=allocation
                        batch['plan']=replace(batch['plan'],allocations=tuple(allocation if a.node_id==allocation.node_id else a
                            for a in batch['plan'].allocations))
                        self.scheduler.placement_active[item.task_id]=batch['plan']
                        if batch['plan'].dispatch is not None:batch['plan'].dispatch['restore_fallback']=str(restore_error)
                        handle=self.runtime.start(allocation,node)
                else:handle = self.runtime.start(allocation, node)
                with self.lock:
                    item.handle = handle
            else:
                self.runtime.health(item.handle)
                self.runtime.reset(item.handle, allocation, node)
            with self.lock:
                item.phase = "READY"
                item.last_acquisition_ms = (monotonic()-started)*1000
                key = self._cost_key(item.runtime_identity,allocation.implementation_id,allocation.processing_node_id,node)
                self.costs.observe(key,allocation.acquisition,item.last_acquisition_ms)
                dispatch = self.batches[item.task_id]['plan'].dispatch
                if dispatch is not None:
                    dispatch['actual_preparation_ms'][allocation.node_id] = item.last_acquisition_ms
                self._save()
            return allocation
        except Exception as exc:
            with self.lock:
                item.error, item.phase = str(exc), "QUARANTINED"
                item.uncertain_start = isinstance(exc, DockerCleanupError)
            raise

    def frame_completed(self, task_id):
        with self.lock:
            self.batches[task_id]['completed_frames'] += 1

    def process(self, allocation, packets):
        with self.lock:
            if self.closed:raise SchedulingError('pool has shut down')
            item = self._leased(allocation)
            if item.phase != "READY":
                raise SchedulingError("pool instance is not ready for a call")
            item.phase = "RUNNING"
        try:
            return self.runtime.process(item.handle, packets)
        finally:
            with self.lock:
                item.phase = "READY"

    def finish(self, plan, *, success, notify, retain_warm=True):
        with self.lock:
            items = [self._leased(a) for a in plan.allocations]
            for item in items:
                if item.phase == "RUNNING":
                    raise SchedulingError("cannot return an executing instance")
                item.phase = "RESETTING"
        warm_ids, errors = set(), []
        for item in reversed(items):
            cleanup_started = monotonic()
            try:
                if item.uncertain_start:
                    raise DockerCleanupError("startup destruction was not confirmed")
                if success and retain_warm and item.handle is not None and (self.max_warm or self._head(item.instance_id) is not None):
                    try:
                        started = monotonic()
                        self.runtime.reset(item.handle, item.allocation, item.node)
                        self.runtime.health(item.handle)
                        key = self._cost_key(item.runtime_identity,item.allocation.implementation_id,
                                             item.allocation.processing_node_id,item.node)
                        self.costs.observe(key,'reset',(monotonic()-started)*1000)
                        warm_ids.add(item.instance_id)
                        continue
                    except Exception as exc:
                        item.error = "reset failed; discarded: " + str(exc)
                if item.handle is not None:
                    self.runtime.stop(item.handle)
                    item.handle = None
            except Exception as exc:
                errors.append(f"{item.instance_id}: {exc}")
                item.error = str(exc)
            finally:
                if plan.dispatch is not None:
                    plan.dispatch.setdefault('actual_cleanup_ms',{})[item.allocation.node_id] = (monotonic()-cleanup_started)*1000
        with self.lock:
            if errors:
                for item in items:
                    item.phase = "QUARANTINED"
                self.condition.notify_all()
                self._save()
                raise DockerCleanupError("pool cleanup failed; placement remains reserved: " + "; ".join(errors))
            self.scheduler.finish_pool_plan(plan.task_id, warm_ids)
            self.batches.pop(plan.task_id,None)
            for item in items:
                item.task_id = item.lease_id = None
                item.last_used = monotonic()
                item.state = "Warm" if item.instance_id in warm_ids else "Release"
                item.phase = "READY" if item.state == "Warm" else "REMOVED"
            self._save()
            self.condition.notify_all()
        notify("pool_returned", warm_count=len(warm_ids), released_count=len(items) - len(warm_ids))
        try:
            self.trim()
        except Exception as exc:
            # The task has ended. A failed idle eviction remains charged and quarantined.
            notify("pool_maintenance_failed", error=str(exc))

    def retry_cleanup(self, task_id):
        with self.lock:
            plan = self.scheduler.placement_active.get(task_id)
            if plan is None or any(self.instances.get(a.container_unit_id) is None or
                    self.instances[a.container_unit_id].phase != "QUARANTINED" for a in plan.allocations):
                raise SchedulingError("task is not a quarantined pool deployment")
        self.finish(plan, success=False, notify=lambda *args, **kwargs: None)

    def close(self):
        with self.lock:
            identifiers = [r.instance_id for r in self.instances.values() if r.state == "Warm"]
        for identifier in identifiers:
            self.evict(identifier)

    def evict(self, instance_id):
        with self.lock:
            if self.closed and not self._shutdown_in_progress:raise SchedulingError('pool is closed')
            item = self.instances.get(instance_id)
            if (item is None or item.state != "Warm" or item.phase not in ("READY", "QUARANTINED")
                    or self._head(instance_id) is not None):
                raise SchedulingError("only an available Warm instance can be evicted")
            item.phase = "DESTROYING"
        try:
            if item.handle is not None:self.runtime.stop(item.handle)
        except Exception as exc:
            with self.lock:
                self._quarantine_warm(item,exc)
                self._save()
            raise
        with self.lock:
            self.scheduler.evict_placement_warm(instance_id)
            item.state, item.phase, item.handle = "Release", "REMOVED", None
            self._save()
            self.condition.notify_all()

    def _quarantine_warm(self,item,error):
        item.phase,item.error='QUARANTINED',str(error)
        record=self.scheduler.placement_instances.get(item.instance_id)
        if record is not None:
            self.scheduler.placement_instances[item.instance_id]=replace(record,resident_demand=item.allocation.demand)

    def reap_expired(self):
        with self.lock:
            expired = [r.instance_id for r in self.instances.values()
                       if r.state == "Warm" and r.phase == "READY"
                       and self._head(r.instance_id) is None
                       and monotonic() - r.last_used >= self.idle_timeout_s]
        for identifier in expired:
            try:
                self.evict(identifier)
            except SchedulingError:
                pass  # Another caller may have acquired it since the snapshot.

    def trim(self):
        while True:
            with self.lock:
                warm = sorted((r for r in self.instances.values() if r.state == "Warm" and r.phase == "READY" and self._head(r.instance_id) is None),
                              key=lambda r: r.last_used)
                if len(warm) <= self.max_warm:
                    # Released instances hold no runtime state. Bound their diagnostic history.
                    released = [r for r in self.instances.values() if r.state == "Release"]
                    for item in released[:-64]:
                        del self.instances[item.instance_id]
                        self.scheduler.placement_instances.pop(item.instance_id, None)
                    return
                victim = warm[0].instance_id
            try:
                self.evict(victim)
            except SchedulingError:
                continue

    def snapshot(self):
        with self.lock:
            records = [{"instance_id": r.instance_id, "container_unit_id": r.instance_id,
                "cid": r.allocation.cid, "implementation_id": r.allocation.implementation_id,
                "processing_node_id": r.allocation.processing_node_id,
                "device_id": r.allocation.device_id, "core_id": r.allocation.core_id,
                "region_id": r.allocation.region_id, "processor": r.allocation.processor,
                "state": r.state, "phase": r.phase, "task_id": r.task_id,
                "lease_id": r.lease_id, "reuse_count": r.reuse_count, "error": r.error,
                "last_acquisition_ms": r.last_acquisition_ms,
                "queue_depth": sum(r.instance_id in request.targets.values() for request in self.pending.values())}
                for r in self.instances.values() if r.state != "Release"]
            return {"scope": "single_backend_process", "lifecycle_states": ["Cold", "Active", "Warm", "Release"],
                "max_warm": self.max_warm, "idle_timeout_s": self.idle_timeout_s,
                "instances": records,
                "queue_mode": "whole_task_fifo", "max_pending": self.max_pending,
                "max_queue_wait_s": self.max_queue_wait_s,
                "pending_tasks": [{"task_id":r.task_id,"owner_task_id":r.owner,
                    "instance_ids":list(r.targets.values()),"waited_ms":(monotonic()-r.entered_at)*1000,
                    "remaining_queue_budget_ms":max(0,(r.wait_until-monotonic())*1000),
                    "resource_reservation":"none_until_dispatch"} for r in self.pending.values()],
                "action_costs": self.costs.snapshot(),
                "checkpoints": [] if self.checkpoints is None else self.checkpoints.list(),
                "checkpoint_supported": self.checkpoints is not None,
                "persistence": dict(enabled=self.journal is not None,error=self.persistence_error,recovery=self.recovery),
                "component_bindings": [{"task_id": r.task_id, "node_id": r.allocation.node_id,
                    "instance_id": r.instance_id, "lease_id": r.lease_id}
                    for r in self.instances.values() if r.state == "Active"],
                "resource_bindings": [{"instance_id": r["instance_id"], "processing_node_id": r["processing_node_id"],
                    "device_id": r["device_id"], "core_id": r["core_id"], "region_id": r["region_id"]} for r in records]}
