from threading import Event
from time import monotonic, sleep
from unittest.mock import patch
import unittest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.repositories import JsonDocumentRepository
from backend.services import TaskService
from deployment.simulation import load_simulation_manager
from deployment.protocol import encode_packet
from examples.heterogeneous_simulation_demo import CONFIG, ROOT, sample_task, sample_frame


class ExecutionProgressTests(unittest.TestCase):
    def setUp(self):
        self.request,self.task,_ = sample_task()
        self.manager=load_simulation_manager(CONFIG)
        service=TaskService(JsonDocumentRepository(ROOT/'examples/TaskDemos'),deployment=self.manager)
        self.app=create_app(service)
        self.client=TestClient(self.app)

    def tearDown(self):
        self.app.state.execution_jobs.close()

    def payload(self):
        return dict(task=self.request,frames=[{k:encode_packet(v) for k,v in sample_frame(self.task).items()} for _ in range(2)])

    def wait(self,job):
        deadline=monotonic()+5
        while monotonic()<deadline:
            state=self.client.get(f'/api/v1/executions/{job}').json()
            if state['state'] in ('COMPLETED','FAILED'): return state
            sleep(.01)
        self.fail('execution did not finish')

    def test_job_reports_actual_progress_before_completion_and_retains_history(self):
        entered,release=Event(),Event()
        original=self.manager.runtime.process
        def blocked(*args):
            entered.set()
            if not release.wait(5): raise RuntimeError('test release timed out')
            return original(*args)
        with patch.object(self.manager.runtime,'process',side_effect=blocked):
            submitted=self.client.post('/api/v1/executions',json=self.payload())
            self.assertEqual(submitted.status_code,202)
            job=submitted.json()['job_id']
            try:
                self.assertTrue(entered.wait(3))
                running=self.client.get(f'/api/v1/executions/{job}').json()
                self.assertEqual(running['state'],'RUNNING')
                self.assertEqual(running['stage'],'processing_node')
                self.assertEqual(running['completed_calls'],0)
                self.assertEqual(running['total_calls'],6)
                self.assertIsNotNone(running['plan'])
                self.assertEqual(self.client.get(f'/api/v1/executions/{job}/result').status_code,409)
            finally:release.set()
            done=self.wait(job)
        self.assertEqual(done['state'],'COMPLETED')
        self.assertEqual(done['completed_calls'],6)
        self.assertEqual(done['completed_frames'],2)
        self.assertTrue(done['resources_released'])
        self.assertEqual({v['completed_frames'] for v in done['nodes'].values()},{2})
        result=self.client.get(f'/api/v1/executions/{job}/result')
        self.assertEqual(result.status_code,200)
        self.assertEqual(len(result.json()['frame_outputs']),2)
        self.assertFalse(self.manager.runtime.instances)

    def test_failure_is_retained_and_resources_are_released(self):
        with patch.object(self.manager.runtime,'process',side_effect=ValueError('injected failure')):
            job=self.client.post('/api/v1/executions',json=self.payload()).json()['job_id']
            done=self.wait(job)
        self.assertEqual(done['state'],'FAILED')
        self.assertIn('injected failure',done['error'])
        self.assertTrue(done['resources_released'])
        self.assertTrue(any(e['event']=='node_failed' for e in done['events']))
        self.assertFalse(self.manager.runtime.instances)
        self.assertFalse(self.manager.scheduler.placement_snapshot()['active_tasks'])

    def test_invalid_payload_and_unknown_job(self):
        self.assertEqual(self.client.post('/api/v1/executions',json={'task':self.request,'frames':[]}).status_code,422)
        self.assertEqual(self.client.get('/api/v1/executions/unknown').status_code,404)


if __name__=='__main__':unittest.main()
