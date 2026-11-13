from dataclasses import replace
import unittest
from unittest.mock import patch
import numpy as np

from deployment.simulation import load_simulation_manager
from examples.heterogeneous_simulation_demo import CONFIG,sample_task,sample_frame
from matching import match_task


class DynamicSchedulerTests(unittest.TestCase):
    def setUp(self):
        _,self.task,self.match=sample_task()
        self.manager=load_simulation_manager(CONFIG)

    def tearDown(self):self.manager.pool.close()

    def execute(self,event=None):
        before={}
        events=[]
        def progress(e):
            events.append(e)
            if e['event']=='rebound':
                before['state_preserved']=all(self.manager.runtime.instances[k][1] is component
                    for k,component in before['objects'].items())
            if e['event']=='rebind_skipped' and 'injected ledger' in e.get('reason',''):
                before['rollback_allocations']=self.manager.scheduler.placement_active[self.task.task_id].allocations
                before['rollback_runtime']={k:v[0].processing_node_id for k,v in self.manager.runtime.instances.items()}
            if e['event']=='frame_completed' and e['frame_index']==1:
                plan=self.manager.scheduler.placement_active[self.task.task_id]
                before['plan']=plan
                before['objects']={k:v[1] for k,v in self.manager.runtime.instances.items()}
                if event=='resource_load':
                    self.manager.dynamic.emit(event,processing_node_id=plan.allocations[-1].processing_node_id,utilization=.99)
                elif event=='priority_change':
                    self.manager.dynamic.emit(event,task_id=self.task.task_id,priority=90)
                elif event=='deadline_risk':
                    self.manager.dynamic.emit(event,task_id=self.task.task_id)
                elif event=='new_task':
                    self.manager.dynamic.emit(event,source_task_id='incoming-demo')
        result=self.manager.execute(self.task,self.match,[sample_frame(self.task)]*4,keep_warm=True,progress=progress)
        return result,before,events

    def test_load_moves_only_one_component_and_preserves_signal_output(self):
        reference,_,_=self.execute()
        self.manager.pool.close()
        result,before,events=self.execute('resource_load')
        changes=[(a,b) for a,b in zip(before['plan'].allocations,result.plan.allocations)
                 if a.processing_node_id!=b.processing_node_id]
        self.assertEqual(len(changes),1)
        self.assertEqual(changes[0][0].node_id,before['plan'].allocations[-1].node_id)
        self.assertEqual(changes[0][0].container_unit_id,changes[0][1].container_unit_id)
        for actual,expected in zip(result.frame_outputs,reference.frame_outputs):
            for node in actual:np.testing.assert_allclose(actual[node].samples,expected[node].samples)
        self.assertTrue(any(e['event']=='rebound' for e in events))
        self.assertTrue(before['state_preserved'])
        self.assertEqual(result.plan.dispatch['rebindings'][-1]['state_transfer'],
                         'same_python_component_object')
        self.assertFalse(self.manager.dynamic.snapshot()['active_tasks'])
        for node in self.manager.resource_snapshot()['nodes']:
            for key,value in node['reserved'].items():
                self.assertEqual(value,sum(p[key] for p in node['reserved_breakdown'].values()))

    def test_state_transfer_failure_keeps_original_plan_and_completes(self):
        with patch.object(self.manager.runtime,'rebind_many',side_effect=ValueError('transfer failure')):
            result,before,events=self.execute('resource_load')
        self.assertEqual(result.plan.allocations,before['plan'].allocations)
        self.assertEqual(len(result.frame_outputs),4)
        self.assertTrue(any(e['event']=='rebind_skipped' for e in events))
        self.assertTrue(any(e.get('status')=='failed' for e in self.manager.dynamic.snapshot()['events']))

    def test_all_four_event_types_are_processed_and_others_are_unchanged(self):
        for kind in ('new_task','priority_change','deadline_risk'):
            with self.subTest(kind=kind):
                self.manager=load_simulation_manager(CONFIG)
                result,before,events=self.execute(kind)
                self.assertEqual(len(result.frame_outputs),4)
                self.assertTrue(any(e['event']=='dynamic_event' for e in events))
                self.assertLessEqual(sum(a.processing_node_id!=b.processing_node_id for a,b in
                    zip(before['plan'].allocations,result.plan.allocations)),1)
                self.manager.pool.close()

    def test_invalid_load_is_rejected_without_changing_state(self):
        for value in (-1,2,float('nan'),'busy'):
            with self.assertRaises(ValueError):
                self.manager.dynamic.emit('resource_load',processing_node_id='sim-cpu',utilization=value)
        self.assertEqual(self.manager.dynamic.loads,{})

    def test_failed_ledger_commit_rolls_back_runtime_and_bindings(self):
        class FailOnce(dict):
            failed=False
            def __setitem__(self,key,value):
                if key in self and not self.failed:
                    self.failed=True
                    raise ValueError('injected ledger commit failure')
                super().__setitem__(key,value)
        self.manager.scheduler.placement_active=FailOnce()
        result,before,events=self.execute('resource_load')
        self.assertEqual(before['rollback_allocations'],before['plan'].allocations)
        self.assertEqual(before['rollback_runtime'],{a.container_unit_id:a.processing_node_id for a in before['plan'].allocations})
        self.assertEqual(len(result.frame_outputs),4)
        self.assertFalse(self.manager.scheduler.placement_snapshot()['active_tasks'])
        self.assertTrue(any(e['event']=='rebind_skipped' for e in events))

    def test_no_available_target_keeps_original_instance(self):
        def progress(event):
            if event['event']=='frame_completed' and event['frame_index']==1:
                for node in self.manager.scheduler.placement_planner.nodes:
                    self.manager.dynamic.emit('resource_load',processing_node_id=node,utilization=.99)
        result=self.manager.execute(self.task,self.match,[sample_frame(self.task)]*3,keep_warm=True,progress=progress)
        self.assertEqual(len(result.frame_outputs),3)
        self.assertTrue(any(e.get('status')=='no_feasible_local_change' for e in self.manager.dynamic.snapshot()['events']))

    def test_priority_reorders_waiting_claims_without_reserving_resources(self):
        from deployment.pool import PendingRequest
        from time import monotonic
        other=replace(self.task,task_id='waiting-other')
        self.manager.dynamic.begin(self.task);self.manager.dynamic.begin(other)
        pool=self.manager.pool
        pool.pending[self.task.task_id]=PendingRequest(self.task.task_id,'owner',None,{},monotonic(),monotonic()+1)
        pool.pending[other.task_id]=PendingRequest(other.task_id,'owner',None,{},monotonic(),monotonic()+1)
        try:
            self.manager.dynamic.emit('priority_change',task_id=other.task_id,priority=90)
            self.assertEqual(list(pool.pending),[other.task_id,self.task.task_id])
            self.assertFalse(self.manager.scheduler.placement_snapshot()['active_tasks'])
        finally:
            pool.pending.clear();self.manager.dynamic.end(self.task.task_id);self.manager.dynamic.end(other.task_id)


class DynamicApiTests(unittest.TestCase):
    def test_event_schema_and_resource_telemetry(self):
        from fastapi.testclient import TestClient
        from backend.api.app import create_app
        from backend.repositories import JsonDocumentRepository
        from backend.services import TaskService
        from examples.heterogeneous_simulation_demo import ROOT
        manager=load_simulation_manager(CONFIG)
        app=create_app(TaskService(JsonDocumentRepository(ROOT/'examples/TaskDemos'),deployment=manager))
        with TestClient(app) as client:
            response=client.post('/api/v1/dynamic/events',json=dict(event='resource_load',processing_node_id='sim-cpu',utilization=.95))
            self.assertEqual(response.status_code,200)
            self.assertEqual(client.get('/api/v1/dynamic').json()['loads']['sim-cpu'],.95)
            self.assertEqual(client.post('/api/v1/dynamic/events',json=dict(event='resource_load',processing_node_id='bad',utilization=.95)).status_code,422)
            self.assertEqual(client.post('/api/v1/dynamic/events',json=dict(event='priority_change',task_id='missing',priority=90)).status_code,409)
