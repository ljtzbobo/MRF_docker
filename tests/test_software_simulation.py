"""Acceptance tests: synthetic accelerator placement and real reference output."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient

from backend.api.app import create_app
from deployment.config import load_deployment_manager
from deployment.protocol import encode_packet
from deployment.simulation import load_simulation_manager
from examples.heterogeneous_simulation_demo import CONFIG, sample_frame, sample_task
from resources.measurements import PhysicalUsage, MeasurementCatalog
from resources.measurements import PhysicalResourceState
from resources.planner import ResourcePlanner
from resources.simulation import load_resource_simulation
from scheduler import SchedulingError
from task_executor import DAGExecutor


class SoftwareSimulationTests(unittest.TestCase):
    def setUp(self):
        self.request, self.task, self.match = sample_task()
        self.manager = load_simulation_manager(CONFIG)

    def test_reference_signal_output_and_released_resources(self):
        before = self.manager.scheduler.placement_snapshot()
        frames = [sample_frame(self.task) for _ in range(4)]
        expected = DAGExecutor.from_match(self.task, self.match).execute_stream(frames)
        with patch('subprocess.run', side_effect=AssertionError('must not invoke Docker')):
            result = self.manager.execute(self.task, self.match, frames)
        self.assertEqual(result.execution_mode, 'software_simulation')
        self.assertEqual(result.plan.profile_basis, 'synthetic')
        self.assertEqual({a.processor for a in result.plan.allocations}, {'dsp', 'fpga'})
        for actual, reference in zip(result.frame_outputs, expected):
            for node, packet in actual.items():
                np.testing.assert_allclose(packet.samples, reference.sink_outputs[node].samples)
        after = self.manager.scheduler.placement_snapshot()
        self.assertEqual(before['node_used'], after['node_used'])
        self.assertEqual(before['link_used'], after['link_used'])
        self.assertFalse(self.manager.runtime.instances)

    def test_all_three_algorithms_use_the_same_model(self):
        for algorithm in ('exact', 'chain_dp', 'heft'):
            with self.subTest(algorithm=algorithm):
                result = self.manager.execute(self.task, self.match, [sample_frame(self.task)], algorithm=algorithm)
                self.assertEqual(result.plan.status, 'FEASIBLE_ESTIMATE')
                self.assertEqual(result.plan.profile_basis, 'synthetic')

    def test_accelerator_capacity_is_reserved_and_competing_task_rejected(self):
        scheduler = self.manager.scheduler
        fixed = dict(zip(self.match.bindings, ('xc7k410t-region-1', 'xc7k410t-region-2', 'xc7k410t-region-3')))
        plan = scheduler.submit_placement(self.task, self.match, fixed, frame_count=4)
        self.assertEqual(scheduler.placement_snapshot()['node_used']['xc7k410t-region-2']['fpga_dsp48'], 240)
        other = replace(self.task, task_id='competing-task')
        other_match = replace(self.match, task_id=other.task_id)
        with self.assertRaises(SchedulingError):
            scheduler.submit_placement(other, other_match, fixed)
        scheduler.complete_placement(self.task.task_id)
        self.assertEqual(scheduler.placement_snapshot()['node_used']['xc7k410t-region-2']['fpga_dsp48'], 0)

    def test_saturated_accelerators_fall_back_to_cpu(self):
        scheduler = self.manager.scheduler
        state = scheduler.placement_baseline
        used = dict(state.node_used)
        for key, node in scheduler.placement_planner.nodes.items():
            if node.processor == 'dsp': used[key] = PhysicalUsage(dsp_compute_mcycles_per_s=1200)
            if node.processor == 'fpga': used[key] = PhysicalUsage(fpga_lut=node.hardware.capacity.fpga_lut)
        scheduler.placement_baseline = replace(state, node_used=used)
        plan = scheduler.preview_placement(self.task, self.match)
        self.assertEqual({a.processor for a in plan.allocations}, {'cpu'})

    def test_single_device_compatibility_planner_does_not_claim_measurement(self):
        planner = self.manager.scheduler.placement_planner
        host = planner.nodes['c66x-core-1'].hardware
        plan = ResourcePlanner().plan_physical(self.task, self.match, host,
            planner.measurements, PhysicalResourceState(host.hardware_id, PhysicalUsage()))
        self.assertEqual(plan.status, 'FEASIBLE_SIMULATION_ESTIMATE')
        self.assertEqual(plan.to_dict()['profile_basis'], 'synthetic')

    def test_input_byte_envelope_is_enforced_with_cleanup(self):
        frame = sample_frame(self.task)
        planner = self.manager.scheduler.placement_planner
        planner.measurements = MeasurementCatalog(tuple(
            replace(profile, input_bytes=32768) if profile.cid == 'C03' else profile
            for profile in planner.measurements.profiles))
        with self.assertRaisesRegex(Exception, 'input exceeds synthetic profile'):
            self.manager.execute(self.task, self.match, [frame])
        self.assertFalse(self.manager.runtime.instances)
        self.assertFalse(self.manager.scheduler.placement_snapshot()['active_tasks'])

    def test_processing_failure_releases_instances_and_reservations(self):
        before = self.manager.scheduler.placement_snapshot()
        with patch.object(self.manager.runtime, 'process', side_effect=ValueError('failure')):
            with self.assertRaisesRegex(Exception, 'failure'):
                self.manager.execute(self.task, self.match, [sample_frame(self.task)])
        self.assertFalse(self.manager.runtime.instances)
        self.assertEqual(before['node_used'], self.manager.scheduler.placement_snapshot()['node_used'])

    def test_snapshot_never_claims_hardware_execution(self):
        scheduler = self.manager.scheduler
        plan = scheduler.submit_placement(self.task, self.match)
        nodes = {n['id']: n for n in self.match.execution_graph['nodes']}
        handles = [self.manager.runtime.start(a, nodes[a.node_id]) for a in plan.allocations]
        snapshot = self.manager.resource_snapshot()
        self.assertEqual(snapshot['execution_mode'], 'software_simulation')
        self.assertTrue(all(c['actual']['execution_backend'] == 'python_cpu_reference' for c in snapshot['containers']))
        for handle in handles:
            self.manager.runtime.stop(handle)
        scheduler.complete_placement(self.task.task_id)

    def test_api_uses_simulation_config_and_reports_mode(self):
        with patch.dict('os.environ', {'MRF_DEPLOYMENT_CONFIG': str(CONFIG)}):
            client = TestClient(create_app())
        self.assertEqual(client.get('/api/v1/deployments/resources').json()['execution_mode'], 'software_simulation')
        response = client.post('/api/v1/deployments/execute', json={
            'task': self.request, 'frames': [{key: encode_packet(p) for key, p in sample_frame(self.task).items()}]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['execution_mode'], 'software_simulation')
        self.assertEqual(response.json()['plan']['profile_basis'], 'synthetic')

    def test_config_requires_explicit_complete_valid_resource_state(self):
        for kind in ('mode', 'capacity', 'state', 'negative', 'links', 'partial_usage'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                raw = json.loads(CONFIG.read_text(encoding='utf-8'))
                raw['profiles'] = str(CONFIG.parent / raw['profiles'])
                if kind == 'mode': raw['mode'] = 'hardware'
                if kind == 'capacity': del raw['nodes'][0]['capacity']['dsp_units']
                if kind == 'state': del raw['initial_state']['node_used']['c66x-core-0']
                if kind == 'negative': raw['initial_state']['node_used']['c66x-core-0']['dsp_compute_mcycles_per_s'] = -1
                if kind == 'links': raw['initial_state']['link_used'][0]['used_bytes_per_s'] = 2_000_000_000
                if kind == 'partial_usage': del raw['initial_state']['node_used']['c66x-core-0']['cpu_millicores']
                path = Path(directory) / 'simulation.json'
                path.write_text(json.dumps(raw), encoding='utf-8')
                with self.assertRaises(ValueError): load_resource_simulation(path)


if __name__ == '__main__':
    unittest.main()
