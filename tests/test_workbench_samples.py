"""Workbench samples can switch tasks in one pool; occupancy explains every unit."""
import json
import unittest

from backend.repositories import JsonDocumentRepository
from backend.services import TaskService
from deployment.protocol import decode_packet
from deployment.simulation import load_simulation_manager
from examples.heterogeneous_simulation_demo import CONFIG, ROOT


class WorkbenchSampleTests(unittest.TestCase):
    def setUp(self):
        self.manager = load_simulation_manager(CONFIG)
        self.service = TaskService(JsonDocumentRepository(ROOT/'examples/TaskDemos'), deployment=self.manager)

    def tearDown(self):
        self.manager.pool.close()

    def run_sample(self, name):
        folder=ROOT/'frontend/public/samples'
        task=json.loads((folder/(name+'-task.json')).read_text(encoding='utf-8'))
        frames=json.loads((folder/(name+'-frames.json')).read_text(encoding='utf-8'))
        self.assertEqual(self.service.plan(task,keep_warm=True)['status'],'FEASIBLE_ESTIMATE')
        return self.service.execute_deployed(task,
            [{key:decode_packet(value) for key,value in frame.items()} for frame in frames],keep_warm=True)

    def assert_breakdown(self):
        for node in self.manager.resource_snapshot()['nodes']:
            for key,value in node['reserved'].items():
                self.assertEqual(value,sum(part[key] for part in node['reserved_breakdown'].values()))
                self.assertGreaterEqual(node['reserved_breakdown']['active'][key],0)

    def test_switch_spectrum_to_data_link_and_reuse_receive_chain(self):
        spectrum=self.run_sample('spectrum-execution')
        self.assertEqual({a.processor for a in spectrum.plan.allocations},{'dsp','fpga'})
        self.assert_breakdown()
        first=self.run_sample('data-link-rx')
        self.assertEqual({a.cid for a in first.plan.allocations},{'C15','C14','C17','C35','C36'})
        self.assertEqual({a.processor for a in first.plan.allocations},{'cpu'})
        self.assertEqual(len(first.frame_outputs),2)
        payload=first.frame_outputs[-1]['rx-02:reassemble'].samples.tobytes()
        self.assertEqual(payload,b'0123456789abcdeffinal')
        second=self.run_sample('data-link-rx')
        self.assertEqual({a.acquisition for a in second.plan.allocations},{'warm_reuse'})
        self.assertEqual(second.frame_outputs[-1]['rx-02:reassemble'].samples.tobytes(),payload)
        self.assertEqual(len(self.manager.pool.snapshot()['instances']),8)
        self.assert_breakdown()
        before=self.manager.resource_snapshot()['nodes']
        self.manager.pool.close()
        after=self.manager.resource_snapshot()['nodes']
        for node in after:
            self.assertEqual(node['reserved'],node['reserved_breakdown']['background'])
            self.assertTrue(all(value==0 for value in node['reserved_breakdown']['warm'].values()))
        self.assertEqual([n['node_id'] for n in before],[n['node_id'] for n in after])


if __name__=='__main__':unittest.main()
