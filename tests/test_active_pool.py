"""Whole-task Active reuse: FIFO, bounded waiting, fallback and state isolation."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from time import monotonic, sleep
import unittest
from unittest.mock import patch

import numpy as np
from deployment.simulation import load_simulation_manager
from examples.heterogeneous_simulation_demo import CONFIG, sample_task, sample_frame
from matching import match_task
from scheduler import SchedulingError


class PoolTestFixture:
    def setUp(self):
        _,self.task,_ = sample_task()
        self.manager = load_simulation_manager(CONFIG)
        self.pool = self.manager.pool
        self.entered,self.release = Event(),Event()
        self.original = self.manager.runtime.process

    def tearDown(self):
        self.release.set()
        self.pool.close()

    def execute(self,name,events=None):
        task=replace(self.task,task_id=name)
        return self.manager.execute(task,match_task(task),[sample_frame(task) for _ in range(4)],
            keep_warm=True,progress=None if events is None else events.append)

    def blocked(self,handle,packets):
        # Synchronize tests without any production sleep or fake processing progress.
        if self.pool.instances[handle].task_id == 'owner':
            self.entered.set()
            if not self.release.wait(5): raise RuntimeError('test gate timed out')
        return self.original(handle,packets)

    def until(self,predicate,timeout=2):
        end=monotonic()+timeout
        while monotonic()<end:
            if predicate():return
            sleep(.005)
        self.fail('expected state did not arrive')


class ActivePoolTests(PoolTestFixture, unittest.TestCase):
    def test_active_queue_reuses_whole_batch_and_does_not_reserve_twice(self):
        events=[]
        with patch.object(self.manager.runtime,'process',side_effect=self.blocked):
            with ThreadPoolExecutor(max_workers=2) as workers:
                owner=workers.submit(self.execute,'owner')
                try:
                    self.assertTrue(self.entered.wait(2))
                    occupied=self.manager.scheduler.placement_snapshot()['node_used']
                    follower=workers.submit(self.execute,'follower',events)
                    self.until(lambda:len(self.pool.snapshot()['pending_tasks'])==1)
                    snapshot=self.pool.snapshot()
                    self.assertEqual(self.manager.scheduler.placement_snapshot()['active_tasks'],['owner'])
                    self.assertEqual(self.manager.scheduler.placement_snapshot()['node_used'],occupied)
                    self.assertEqual(snapshot['pending_tasks'][0]['resource_reservation'],'none_until_dispatch')
                    self.assertTrue(all(row['queue_depth']==1 for row in snapshot['instances']))
                    self.assertFalse(follower.done())
                finally:self.release.set()
                first,second=owner.result(),follower.result()
        self.assertEqual([a.container_unit_id for a in first.plan.allocations],
                         [a.container_unit_id for a in second.plan.allocations])
        self.assertEqual({a.acquisition for a in second.plan.allocations},{'active_reuse'})
        self.assertGreater(second.plan.dispatch['actual_wait_ms'],0)
        self.assertTrue(all(a.lease_id!=b.lease_id for a,b in zip(first.plan.allocations,second.plan.allocations)))
        for a,b in zip(first.frame_outputs,second.frame_outputs):
            for node in a: np.testing.assert_allclose(a[node].samples,b[node].samples)
        self.assertFalse(self.pool.snapshot()['pending_tasks'])
        self.assertTrue(any(e['event']=='waiting_for_instance' for e in events))
        self.assertEqual(len(second.plan.dispatch['actual_preparation_ms']),3)
        self.assertTrue(any(c['action']=='active_reuse' for c in self.pool.snapshot()['action_costs']))

    def test_predicted_deadline_risk_selects_another_instance_without_waiting(self):
        with patch.object(self.manager.runtime,'process',side_effect=self.blocked):
            with ThreadPoolExecutor(max_workers=2) as workers:
                owner=workers.submit(self.execute,'owner')
                try:
                    self.assertTrue(self.entered.wait(2))
                    with patch.object(self.pool,'_remaining_ms',return_value=10000):
                        other=self.execute('urgent')
                    self.assertEqual({a.acquisition for a in other.plan.allocations},{'cold_start'})
                    self.assertFalse(self.pool.snapshot()['pending_tasks'])
                    self.assertTrue(any(d['action']=='active_reuse' and not d['feasible'] and
                        d['reason']=='queue_and_execution_exceed_deadline' for d in other.plan.dispatch['decisions']))
                    self.assertFalse(owner.done())
                finally:self.release.set()
                owner.result()

    def test_actual_wait_timeout_replans_elsewhere(self):
        self.pool.max_queue_wait_s=.04
        with patch.object(self.manager.runtime,'process',side_effect=self.blocked):
            with ThreadPoolExecutor(max_workers=2) as workers:
                owner=workers.submit(self.execute,'owner')
                try:
                    self.assertTrue(self.entered.wait(2))
                    other=self.execute('wait-timeout')
                    self.assertEqual({a.acquisition for a in other.plan.allocations},{'cold_start'})
                    self.assertGreaterEqual(other.plan.dispatch['actual_wait_ms'],30)
                    self.assertTrue(any(d['action']=='queue_fallback' and d['reason']=='queue_budget_exhausted'
                                        for d in other.plan.dispatch['decisions']))
                    self.assertFalse(self.pool.snapshot()['pending_tasks'])
                    self.assertFalse(owner.done())
                finally:self.release.set()
                owner.result()

    def test_timeout_prefers_available_warm_before_cold_start(self):
        with patch.object(self.manager.runtime,'process',side_effect=self.blocked):
            with ThreadPoolExecutor(max_workers=2) as workers:
                owner=workers.submit(self.execute,'owner')
                try:
                    self.assertTrue(self.entered.wait(2))
                    self.pool.active_reuse_enabled=False
                    spare=self.execute('spare-warm')
                    self.pool.active_reuse_enabled=True
                    self.pool.max_queue_wait_s=.05
                    result=self.execute('warm-fallback')
                    self.assertEqual({a.acquisition for a in result.plan.allocations},{'warm_reuse'})
                    self.assertEqual({a.container_unit_id for a in result.plan.allocations},
                                     {a.container_unit_id for a in spare.plan.allocations})
                    self.assertTrue(any(d['action']=='queue_fallback' for d in result.plan.dispatch['decisions']))
                finally:self.release.set()
                owner.result()

    def test_fifo_three_tasks_and_pending_task_id_is_unique(self):
        completed=[]
        def execute(name):
            result=self.execute(name)
            completed.append(name)
            return result
        with patch.object(self.manager.runtime,'process',side_effect=self.blocked):
            with ThreadPoolExecutor(max_workers=3) as workers:
                owner=workers.submit(execute,'owner')
                try:
                    self.assertTrue(self.entered.wait(2))
                    second=workers.submit(execute,'second')
                    self.until(lambda:len(self.pool.snapshot()['pending_tasks'])==1)
                    with self.assertRaisesRegex(SchedulingError,'already active or queued'):
                        self.execute('second')
                    third=workers.submit(execute,'third')
                    self.until(lambda:len(self.pool.snapshot()['pending_tasks'])==2)
                    self.assertEqual([r['task_id'] for r in self.pool.snapshot()['pending_tasks']],['second','third'])
                finally:self.release.set()
                results=[f.result() for f in (owner,second,third)]
        self.assertEqual(completed,['owner','second','third'])
        self.assertEqual(len({tuple(a.container_unit_id for a in r.plan.allocations) for r in results}),1)

    def test_failed_owner_wakes_waiter_and_uses_clean_new_instances(self):
        def failure(handle,packets):
            if self.pool.instances[handle].task_id=='owner':
                self.entered.set()
                if not self.release.wait(5):raise RuntimeError('gate timeout')
                raise ValueError('owner failed')
            return self.original(handle,packets)
        with patch.object(self.manager.runtime,'process',side_effect=failure):
            with ThreadPoolExecutor(max_workers=2) as workers:
                owner=workers.submit(self.execute,'owner')
                try:
                    self.assertTrue(self.entered.wait(2))
                    follower=workers.submit(self.execute,'after-failure')
                    self.until(lambda:len(self.pool.snapshot()['pending_tasks'])==1)
                finally:self.release.set()
                with self.assertRaisesRegex(Exception,'owner failed'):owner.result()
                result=follower.result()
        self.assertEqual({a.acquisition for a in result.plan.allocations},{'cold_start'})
        self.assertTrue(any(d['reason']=='owner_failed_or_instance_unavailable'
                            for d in result.plan.dispatch['decisions'] if d['action']=='queue_fallback'))
        self.assertFalse(self.manager.scheduler.placement_snapshot()['active_tasks'])
        self.assertFalse(self.pool.snapshot()['pending_tasks'])

    def test_warm_preparation_cost_has_observation_basis(self):
        first=self.execute('first')
        task=replace(self.task,task_id='second')
        fixed={a.node_id:a.processing_node_id for a in first.plan.allocations}
        second=self.manager.execute(task,match_task(task),[sample_frame(task) for _ in range(4)],fixed,keep_warm=True)
        self.assertEqual({a.container_unit_id for a in first.plan.allocations},
                         {a.container_unit_id for a in second.plan.allocations})
        self.assertTrue(all(a.acquisition_cost_basis=='observed_reset_recent_max' for a in second.plan.allocations))
        self.assertAlmostEqual(sum(a.estimated_acquisition_ms for a in second.plan.allocations)+
            second.plan.frame_count*(sum(a.runtime_p95_ms for a in second.plan.allocations)+sum(second.plan.edge_transfer_ms.values())),
            second.plan.estimated_completion_ms)
        self.assertTrue(all(value>=0 for value in second.plan.dispatch['actual_preparation_ms'].values()))

    def test_slow_decision_does_not_admit_an_expired_task(self):
        planner=self.manager.scheduler.placement_planner
        original=planner.plan
        def slow(*args,**kwargs):
            result=original(*args,**kwargs)
            sleep(.06)  # Simulate an expensive planner, not signal processing.
            return result
        task=replace(self.task,task_id='decision-expired',X={**self.task.X,'remaining_deadline_ms':50})
        with patch.object(planner,'plan',side_effect=slow):
            with self.assertRaisesRegex(SchedulingError,'decision_time_exhausts_deadline'):
                self.manager.execute(task,match_task(task),[sample_frame(task)]*4,keep_warm=True)
        self.assertFalse(self.manager.scheduler.placement_snapshot()['active_tasks'])
        self.assertFalse(self.manager.runtime.instances)

    def test_waiter_cannot_force_release_a_claimed_warm_instance(self):
        # A queued handoff must remain protected even between owner completion and dispatch.
        from deployment.pool import PendingRequest
        result=self.execute('seed')
        a=result.plan.allocations[0]
        self.pool.pending['claim']=PendingRequest('claim','seed',result.plan,
            {a.node_id:a.container_unit_id},monotonic(),monotonic()+1)
        try:
            with self.assertRaises(SchedulingError):self.pool.evict(a.container_unit_id)
            self.pool.instances[a.container_unit_id].last_used-=120
            self.pool.reap_expired()
            self.assertEqual(self.pool.instances[a.container_unit_id].state,'Warm')
        finally:self.pool.pending.clear()

    def test_another_instance_runs_while_owner_kernel_is_blocked(self):
        runtime=self.manager.runtime
        start=runtime.start
        def gated_start(allocation,node):
            handle=start(allocation,node)
            if self.pool.instances[handle].task_id=='owner' and node['cid']=='C03':
                component=runtime.instances[handle][1]
                process=component.process
                def gate(packet):
                    self.entered.set()
                    if not self.release.wait(5):raise RuntimeError('kernel gate timeout')
                    return process(packet)
                component.process=gate
            return handle
        with patch.object(runtime,'start',side_effect=gated_start):
            with ThreadPoolExecutor(max_workers=2) as workers:
                owner=workers.submit(self.execute,'owner')
                try:
                    self.assertTrue(self.entered.wait(2))
                    with patch.object(self.pool,'_remaining_ms',return_value=10000):
                        follower=workers.submit(self.execute,'separate-kernel')
                        result=follower.result(timeout=2)
                    self.assertEqual({a.acquisition for a in result.plan.allocations},{'cold_start'})
                    self.assertFalse(owner.done())
                finally:self.release.set()
                owner.result()


class ActivePoolApiTests(PoolTestFixture, unittest.TestCase):
    def test_async_api_reports_waiting_and_completed_action_costs(self):
        from copy import deepcopy
        from fastapi.testclient import TestClient
        from backend.api.app import create_app
        from backend.repositories import JsonDocumentRepository
        from backend.services import TaskService
        from deployment.protocol import encode_packet
        from examples.heterogeneous_simulation_demo import ROOT
        request,_,_=sample_task()
        frames=[{key:encode_packet(value) for key,value in sample_frame(self.task).items()} for _ in range(4)]
        service=TaskService(JsonDocumentRepository(ROOT/'examples/TaskDemos'),deployment=self.manager)
        app=create_app(service)
        with patch.object(self.manager.runtime,'process',side_effect=self.blocked),TestClient(app) as client:
            def submit(name):
                task=deepcopy(request);task['task_id']=name
                response=client.post('/api/v1/executions',json=dict(task=task,frames=frames,keep_warm=True))
                self.assertEqual(response.status_code,202,response.text)
                return response.json()['job_id']
            first=submit('owner')
            try:
                self.assertTrue(self.entered.wait(2))
                second=submit('api-follower')
                def waiting():
                    state=client.get('/api/v1/executions/'+second).json()
                    return state['stage']=='waiting_for_instance'
                self.until(waiting)
                state=client.get('/api/v1/executions/'+second).json()
                self.assertGreaterEqual(state['waiting_ms'],0)
                self.assertEqual(state['plan']['dispatch']['reservation_status'],'preview')
                self.assertEqual(client.get('/api/v1/pool').json()['pending_tasks'][0]['task_id'],'api-follower')
            finally:self.release.set()
            self.until(lambda:all(client.get('/api/v1/executions/'+j).json()['state']=='COMPLETED' for j in (first,second)))
            result=client.get('/api/v1/executions/'+second+'/result').json()
            self.assertEqual({a['acquisition'] for a in result['plan']['allocations']},{'active_reuse'})
            self.assertEqual(len(result['plan']['dispatch']['actual_preparation_ms']),3)
            self.assertEqual(len(result['plan']['dispatch']['actual_cleanup_ms']),3)


if __name__=='__main__':unittest.main()
