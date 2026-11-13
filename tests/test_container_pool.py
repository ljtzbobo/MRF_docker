"""Stage-one acceptance: exclusive cross-task reuse and one resource ledger."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from unittest.mock import patch
import unittest

import numpy as np
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.repositories import JsonDocumentRepository
from backend.services import TaskService
from deployment.protocol import encode_packet
from deployment.simulation import load_simulation_manager
from examples.heterogeneous_simulation_demo import CONFIG, ROOT, sample_frame, sample_task
from matching import match_task
from scheduler import SchedulingError


class ContainerPoolTests(unittest.TestCase):
    def setUp(self):
        self.request, self.task, self.match = sample_task()
        self.manager = load_simulation_manager(CONFIG)
        self.before = self.manager.scheduler.placement_snapshot()

    def execute(self, name, *, algorithm='exact', fixed=None):
        task = replace(self.task, task_id=name)
        return self.manager.execute(task, match_task(task), [sample_frame(self.task) for _ in range(4)],
                                    fixed, keep_warm=True, algorithm=algorithm)

    def test_cross_task_reuse_resets_components_and_preserves_output(self):
        first = self.execute('first')
        old_objects = {k: value[1] for k,value in self.manager.runtime.instances.items()}
        with patch.object(self.manager.runtime, 'start', wraps=self.manager.runtime.start) as start:
            second = self.execute('second')
            self.assertEqual(start.call_count, 0)
        self.assertEqual([a.container_unit_id for a in first.plan.allocations],
                         [a.container_unit_id for a in second.plan.allocations])
        self.assertTrue(all(a.acquisition == 'warm_reuse' for a in second.plan.allocations))
        self.assertTrue(all(a.lease_id != b.lease_id for a,b in zip(first.plan.allocations,second.plan.allocations)))
        self.assertTrue(all(value[1] is not old_objects[k] for k,value in self.manager.runtime.instances.items()))
        for left,right in zip(first.frame_outputs,second.frame_outputs):
            for node in left:
                np.testing.assert_allclose(left[node].samples,right[node].samples)
        snapshot = self.manager.resource_snapshot()
        self.assertFalse(snapshot['active_tasks'])
        self.assertFalse(snapshot['pool']['component_bindings'])
        self.assertEqual(len(snapshot['pool']['resource_bindings']),3)
        self.assertEqual({r['state'] for r in snapshot['containers']},{'Warm'})
        self.assertEqual({r['reuse_count'] for r in snapshot['containers']},{1})
        for a in second.plan.allocations:
            reserved = next(n['reserved'] for n in snapshot['nodes'] if n['node_id'] == a.processing_node_id)
            initial = self.before['node_used'][a.processing_node_id]
            if a.processor == 'fpga': self.assertEqual(reserved['fpga_lut'], initial['fpga_lut'] + a.demand.fpga_lut)
            if a.processor == 'dsp':
                self.assertEqual(reserved['dsp_compute_mcycles_per_s'],initial['dsp_compute_mcycles_per_s'])
                self.assertEqual(reserved['dsp_local_memory_bytes'],initial['dsp_local_memory_bytes'] + a.demand.dsp_local_memory_bytes)
        with self.assertRaisesRegex(SchedulingError,'stale'):
            self.manager.pool.process(first.plan.allocations[0], {})

    def test_full_fpga_region_can_be_reused_without_double_reservation_for_each_algorithm(self):
        fixed = dict(zip(self.match.execution_graph['topological_order'],
                         ('c66x-core-0','xc7k410t-region-0','xc7k410t-region-1')))
        planner = self.manager.scheduler.placement_planner
        # An allocated implementation exactly fills its configured LUT capacity.
        probe = self.manager.scheduler.preview_placement(self.task,self.match,fixed,frame_count=4)
        for a in probe.allocations:
            if a.processor == 'fpga':
                node = planner.nodes[a.processing_node_id]
                planner.nodes[a.processing_node_id] = replace(node,hardware=replace(node.hardware,
                    capacity=replace(node.hardware.capacity,fpga_lut=a.demand.fpga_lut
                        + self.before['node_used'][a.processing_node_id]['fpga_lut'])))
        first = self.execute('full-first',fixed=fixed)
        baseline = self.manager.scheduler.placement_snapshot()['node_used']
        for algorithm in ('exact','chain_dp','heft'):
            result = self.execute('full-'+algorithm,algorithm=algorithm,fixed=fixed)
            self.assertEqual({a.acquisition for a in result.plan.allocations},{'warm_reuse'})
            self.assertEqual(self.manager.scheduler.placement_snapshot()['node_used'],baseline)
        for a in first.plan.allocations: self.manager.pool.evict(a.container_unit_id)
        self.assertEqual(self.manager.scheduler.placement_snapshot()['node_used'],self.before['node_used'])
        self.assertFalse(self.manager.runtime.instances)

    def test_concurrent_tasks_never_borrow_the_same_instance(self):
        self.manager.pool.active_reuse_enabled = False  # Legacy exclusive policy remains available.
        first = self.execute('seed')
        entered,release = Event(),Event()
        original = self.manager.runtime.process
        def blocked(*args):
            entered.set()
            if not release.wait(10): raise RuntimeError('test timeout')
            return original(*args)
        with patch.object(self.manager.runtime,'process',side_effect=blocked):
            with ThreadPoolExecutor(max_workers=2) as workers:
                a = workers.submit(self.execute,'parallel-a')
                try:
                    self.assertTrue(entered.wait(5))
                    snapshot = self.manager.pool.snapshot()
                    leased_ids = {r['instance_id'] for r in snapshot['component_bindings']}
                    self.assertEqual(len(leased_ids),3)
                    with self.assertRaisesRegex(SchedulingError,'stale'):
                        self.manager.pool.process(first.plan.allocations[0],{})
                    with self.assertRaises(SchedulingError): self.manager.pool.evict(next(iter(leased_ids)))
                    b = workers.submit(self.execute,'parallel-b')
                    # Wait for the second task's plan without slowing component execution artificially.
                    from time import monotonic, sleep
                    deadline=monotonic()+5
                    while monotonic()<deadline:
                        bindings=self.manager.pool.snapshot()['component_bindings']
                        if len(bindings)==6: break
                        sleep(.01)
                    self.assertEqual(len(bindings),6)
                    self.assertEqual(len({r['instance_id'] for r in bindings}),6)
                finally: release.set()
                left,right = a.result(),b.result()
        self.assertTrue({x.container_unit_id for x in left.plan.allocations}.isdisjoint(
                        {x.container_unit_id for x in right.plan.allocations}))

    def test_failure_discards_instances_and_releases_resources(self):
        self.execute('seed')
        with patch.object(self.manager.runtime,'process',side_effect=ValueError('injected process failure')):
            with self.assertRaisesRegex(Exception,'injected process failure'): self.execute('broken')
        self.assertFalse(self.manager.scheduler.placement_snapshot()['active_tasks'])
        # Unborrowed Warm units may remain when measured preparation costs favor a new host.
        warm={r['instance_id'] for r in self.manager.pool.snapshot()['instances'] if r['state']=='Warm'}
        self.assertEqual(set(self.manager.runtime.instances),warm)
        for node in self.manager.resource_snapshot()['nodes']:
            self.assertTrue(all(value==0 for value in node['reserved_breakdown']['active'].values()))

    def test_reset_failure_destroys_instead_of_reusing_dirty_state(self):
        with patch.object(self.manager.runtime,'reset',side_effect=ValueError('dirty reset')):
            self.execute('reset-fails')
        self.assertFalse(self.manager.runtime.instances)
        self.assertFalse(self.manager.pool.snapshot()['instances'])
        self.assertEqual(self.manager.scheduler.placement_snapshot()['node_used'],self.before['node_used'])

    def test_unconfirmed_cleanup_keeps_reservations_and_quarantines(self):
        with patch.object(self.manager.runtime,'process',side_effect=ValueError('process failure')), \
             patch.object(self.manager.runtime,'stop',side_effect=ValueError('stop failure')):
            with self.assertRaisesRegex(Exception,'remains reserved'): self.execute('cleanup-fails')
        self.assertEqual(self.manager.scheduler.placement_snapshot()['active_tasks'],['cleanup-fails'])
        self.assertTrue(all(r['phase']=='QUARANTINED' for r in self.manager.pool.snapshot()['instances']))
        self.manager.pool.retry_cleanup('cleanup-fails')
        self.assertFalse(self.manager.runtime.instances)
        self.assertEqual(self.manager.scheduler.placement_snapshot()['node_used'],self.before['node_used'])

    def test_expiry_and_pool_limit_destroy_and_update_ledger(self):
        self.manager.pool.max_warm=1
        self.execute('limited')
        self.assertEqual(len(self.manager.pool.snapshot()['instances']),1)
        item=next(r for r in self.manager.pool.instances.values() if r.state=='Warm')
        item.last_used-=120
        self.manager.pool.reap_expired()
        self.assertFalse(self.manager.runtime.instances)
        self.assertEqual(self.manager.scheduler.placement_snapshot()['node_used'],self.before['node_used'])

    def test_runtime_version_change_prevents_reuse(self):
        first=self.execute('version-one')
        with patch.object(self.manager.runtime,'pool_identity',return_value='new-runtime-version'):
            second=self.execute('version-two')
        self.assertTrue({a.container_unit_id for a in first.plan.allocations}.isdisjoint(
                        {a.container_unit_id for a in second.plan.allocations}))
        self.assertEqual({a.acquisition for a in second.plan.allocations},{'cold_start'})

    def test_api_exposes_pool_and_rejects_non_boolean_policy(self):
        service=TaskService(JsonDocumentRepository(ROOT/'examples/TaskDemos'),deployment=self.manager)
        app=create_app(service)
        try:
            client=TestClient(app)
            payload={'task':self.request,'frames':[{k:encode_packet(v) for k,v in sample_frame(self.task).items()}],
                     'keep_warm':True}
            first=client.post('/api/v1/deployments/execute',json=payload)
            self.assertEqual(first.status_code,200,first.text)
            second=client.post('/api/v1/deployments/execute',json=payload)
            self.assertEqual(second.status_code,200,second.text)
            self.assertEqual({a['acquisition'] for a in second.json()['plan']['allocations']},{'warm_reuse'})
            self.assertEqual(client.post('/api/v1/plans?keep_warm=true',json=self.request).status_code,200)
            identifier=client.get('/api/v1/pool').json()['instances'][0]['instance_id']
            self.assertEqual(client.post(f'/api/v1/pool/instances/{identifier}/release').status_code,200)
            self.assertEqual(client.post('/api/v1/executions',json={**payload,'keep_warm':'true'}).status_code,422)
            submitted=client.post('/api/v1/executions',json=payload)
            self.assertEqual(submitted.status_code,202,submitted.text)
            from time import monotonic, sleep
            deadline=monotonic()+5
            while monotonic()<deadline:
                job=client.get('/api/v1/executions/'+submitted.json()['job_id']).json()
                if job['state'] in ('COMPLETED','FAILED'): break
                sleep(.01)
            self.assertEqual(job['state'],'COMPLETED',job.get('error'))
            self.assertTrue(any(e['event']=='pool_returned' for e in job['events']))
        finally: app.state.execution_jobs.close()

    def test_http_worker_reset_reconstructs_component_and_keeps_transport(self):
        from http.server import ThreadingHTTPServer
        from threading import Thread
        from components.implementations.runtime_registry import create_runtime_component
        from deployment.docker_runtime import DockerRuntime, ContainerHandle
        from deployment.worker import make_handler
        from task_repr.template.refinement import SpecificationNode
        node=self.match.execution_graph['nodes'][0]
        component=create_runtime_component(node['cid'],**node['parameters'])
        spec=SpecificationNode(node['id'],node['id'],'worker',node['inputs'],node['outputs'],{},node['id'])
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(component,spec))
        thread=Thread(target=server.serve_forever,daemon=True)
        thread.start()
        runtime=DockerRuntime('test',{'C03':'test-image'})
        handle=ContainerHandle('test-container',f'http://127.0.0.1:{server.server_port}','test-unit')
        packets={next(iter(node['inputs'])): sample_frame(self.task)[node['id']]}
        try:
            runtime.health(handle)
            first=runtime.process(handle,packets)
            runtime.reset(handle,None,node)
            second=runtime.process(handle,packets)
            np.testing.assert_allclose(first.samples,second.samples)
        finally:
            server.shutdown();server.server_close();thread.join(timeout=2)


if __name__=='__main__': unittest.main()
