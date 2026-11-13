from dataclasses import asdict
import unittest

from deployment.simulation import load_simulation_manager
from deployment.docker_runtime import DockerRuntime
from deployment.inventory import component_inventory
from examples.heterogeneous_simulation_demo import CONFIG, sample_task


class ResourceInventoryTests(unittest.TestCase):
    def setUp(self):
        self.manager = load_simulation_manager(CONFIG)

    def test_reference_inventory_has_available_budgets_and_creation_candidates(self):
        snapshot = self.manager.resource_snapshot()
        self.assertEqual(snapshot['execution_mode'], 'software_simulation')
        self.assertTrue(snapshot['idle_container_pool_supported'])
        self.assertEqual(snapshot['pool']['scope'], 'single_backend_process')
        self.assertFalse(snapshot['containers'])
        self.assertEqual(len(snapshot['nodes']), 9)
        nodes = {n['node_id']: n for n in snapshot['nodes']}
        self.assertEqual(nodes['sim-cpu']['available']['cpu_millicores'], 7500)
        self.assertEqual(nodes['c66x-core-0']['available']['dsp_compute_mcycles_per_s'], 1100)
        self.assertEqual(nodes['xc7k410t-region-0']['available']['fpga_lut'], 42840)
        self.assertEqual({c['cid'] for c in snapshot['component_templates']},
                         {'C03', 'C05', 'C27', 'C15', 'C14', 'C17', 'C35', 'C36'})
        for item in snapshot['component_templates']:
            self.assertEqual(item['creation_status'], 'SOFTWARE_REFERENCE')
            implementations = {i['processor']: i for i in item['implementations']}
            if item['cid'] in {'C03', 'C05', 'C27'}:
                self.assertEqual(len(implementations['dsp']['processing_nodes']), 4)
                self.assertEqual(len(implementations['fpga']['processing_nodes']), 4)
            else:
                self.assertEqual(set(implementations), {'cpu'})
            self.assertEqual(len(implementations['cpu']['processing_nodes']), 1)

    def test_available_resources_follow_task_reservation_and_release(self):
        _, task, match = sample_task()
        before = self.manager.resource_snapshot()
        plan = self.manager.scheduler.submit_placement(task, match, frame_count=4)
        during = self.manager.resource_snapshot()
        for node in during['nodes']:
            for field, capacity in node['capacity'].items():
                self.assertEqual(node['available'][field], capacity-node['reserved'][field])
                self.assertEqual(node['reserved_breakdown']['active'][field],
                                 getattr(plan.node_demand[node['node_id']],field))
                self.assertEqual(node['reserved_breakdown']['warm'][field],0)
        self.assertEqual(len(during['containers']), len(plan.allocations))
        self.manager.scheduler.complete_placement(task.task_id)
        after = self.manager.resource_snapshot()
        self.assertEqual(before['nodes'], after['nodes'])
        self.assertFalse(after['containers'])

    def test_docker_image_declarations_are_not_reported_as_verified_containers(self):
        runtime = DockerRuntime('local', {'C03': 'example:local', 'C08': 'fft:local'})
        rows = component_inventory(self.manager.scheduler.placement_planner, runtime)
        indexed = {r['cid']: r for r in rows}
        self.assertEqual(indexed['C03']['creation_status'], 'IMAGE_CONFIGURED')
        self.assertEqual(indexed['C08']['creation_status'], 'NEEDS_CALIBRATION')
        self.assertTrue(all(not r['image_verified'] for r in rows))


if __name__ == '__main__':
    unittest.main()
