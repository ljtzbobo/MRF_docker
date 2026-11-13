from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from deployment.session import DeploymentManager
from deployment.simulation import SoftwareSimulationRuntime,load_simulation_manager
from resources.simulation import load_resource_simulation
from scheduler import TaskScheduler,SchedulingError
from examples.heterogeneous_simulation_demo import CONFIG,sample_task,sample_frame
from matching import match_task


class SurvivingRuntime(SoftwareSimulationRuntime):
    """Test double for Docker daemon objects surviving a backend process exit."""
    def _docker(self,*args):return 'test-engine'
    def reconcile_handle(self,a):
        return (a.container_unit_id,True) if a.container_unit_id in self.instances else (None,False)


class PoolPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.path=Path(self.folder.name)/'pool.sqlite'
        _,self.task,self.match=sample_task()
        self.managers=[]

    def tearDown(self):
        for manager in reversed(self.managers):
            if not manager.pool.closed:
                manager.pool.close();manager.pool.shutdown()
        self.folder.cleanup()

    def manager(self,surviving=True,previous=None):
        env=load_resource_simulation(CONFIG)
        runtime=(SurvivingRuntime if surviving else SoftwareSimulationRuntime)(env.planner)
        if previous is not None:
            runtime.instances=previous.runtime.instances
            runtime._unit_locks=previous.runtime._unit_locks
        manager=DeploymentManager(TaskScheduler(placement_planner=env.planner,placement_baseline=env.initial_state),runtime)
        self.managers.append(manager)
        manager.pool.configure_persistence(self.path)
        return manager

    @staticmethod
    def crash(manager):
        manager.pool.journal.close()
        manager.pool.closed=True

    def test_warm_is_reconciled_and_old_backend_is_fenced(self):
        first=self.manager()
        result=first.execute(self.task,self.match,[sample_frame(self.task)],keep_warm=True)
        identifiers={a.container_unit_id for a in result.plan.allocations}
        self.crash(first)
        second=self.manager(previous=first)
        self.assertEqual(set(second.pool.recovery['adopted_warm']),identifiers)
        with self.assertRaises(SchedulingError):first.pool.evict(next(iter(identifiers)))
        task=replace(self.task,task_id='after-restart')
        fixed={a.node_id:a.processing_node_id for a in result.plan.allocations}
        new=second.execute(task,match_task(task),[sample_frame(task)],fixed,keep_warm=True)
        self.assertEqual({a.container_unit_id for a in new.plan.allocations},identifiers)
        self.assertTrue(all(a.acquisition=='warm_reuse' for a in new.plan.allocations))

    def test_active_is_quarantined_with_budget_and_new_lease_until_cleanup(self):
        first=self.manager()
        plan=first.pool.reserve(self.task,self.match,frame_count=1)
        nodes={n['id']:n for n in self.match.execution_graph['nodes']}
        for a in plan.allocations:first.pool.start(a,nodes[a.node_id])
        before=first.scheduler.placement_snapshot()['node_used']
        self.crash(first)
        second=self.manager(previous=first)
        self.assertEqual(second.scheduler.placement_snapshot()['node_used'],before)
        self.assertTrue(all(r['phase']=='QUARANTINED' for r in second.pool.snapshot()['instances']))
        with self.assertRaises(SchedulingError):second.pool.process(plan.allocations[0],{})
        second.pool.retry_cleanup(self.task.task_id)
        self.assertFalse(second.runtime.instances)
        self.assertFalse(second.scheduler.placement_snapshot()['active_tasks'])

    def test_second_owner_is_rejected(self):
        first=self.manager()
        with self.assertRaisesRegex(SchedulingError,'another backend'):
            self.manager()

    def test_nonretained_tasks_are_still_journaled(self):
        first=self.manager()
        observations=[]
        def progress(event):
            if event['event']=='node_ready':
                observations.append(first.pool.journal.read()['plans'])
        first.execute(self.task,self.match,[sample_frame(self.task)],keep_warm=False,progress=progress)
        self.assertTrue(observations and all(observations))
        self.assertFalse(first.pool.snapshot()['instances'])
        self.assertFalse(first.runtime.instances)
        self.assertFalse(first.pool.journal.read()['plans'])

    def test_reference_objects_are_not_claimed_to_survive_restart(self):
        first=self.manager(surviving=False)
        first.execute(self.task,self.match,[sample_frame(self.task)],keep_warm=True)
        self.crash(first)
        second=self.manager(surviving=False)
        self.assertEqual(len(second.pool.recovery['in_memory_lost']),3)
        self.assertFalse(second.runtime.instances)
        self.assertFalse(second.pool.snapshot()['instances'])
