from pathlib import Path
import tempfile
import unittest

import numpy as np
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.repositories import InvalidDocument, JsonDocumentRepository
from backend.services import ResourceService, TaskService
from components.runtime import DataPacket
from deployment.protocol import encode_packet
from examples.data_link_receive_execution_demo import aligned_iq_frame
from task_executor import DAGExecutionError


ROOT = Path(__file__).resolve().parents[1]


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.tasks = TaskService(JsonDocumentRepository(ROOT / "examples" / "TaskDemos"))
        self.resources = ResourceService(JsonDocumentRepository(ROOT / "examples" / "ResourceDemos"))
        self.client = TestClient(create_app(self.tasks, self.resources))

    def test_tasks_and_resources_are_loaded_independently(self):
        self.assertIn("signal_analysis_task", self.client.get("/api/v1/tasks").json()["items"])
        self.assertEqual(self.client.get("/api/v1/resources").json()["items"], ["virtual_lab"])
        self.assertEqual(self.client.get("/api/v1/resources/virtual_lab").json()["resource_id"],
                         "virtual-lab")
        self.assertEqual(self.client.get("/api/v1/tasks/signal_analysis_task").json()["task_id"],
                         "signal-analysis-001")
        self.assertEqual(self.client.get("/api/v1/deployments/resources").json(),
                         {"configured": False, "nodes": [], "active_tasks": [], "containers": []})

    def test_validate_and_match_saved_or_submitted_task(self):
        document = self.tasks.get("signal_analysis_task")
        self.assertEqual(self.client.get("/api/v1/health").json(), {"status": "ok"})
        self.assertEqual(self.client.post("/api/v1/tasks/validate", json=document).json(),
                         {"task_id": "signal-analysis-001", "node_count": 3, "edge_count": 2})
        for response in (self.client.post("/api/v1/tasks/signal_analysis_task/match"),
                         self.client.post("/api/v1/matches", json=document)):
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "MATCHED")
            self.assertEqual([binding["cid"] for binding in response.json()["bindings"].values()],
                             ["C03", "C05", "C27"])

    def test_errors_are_mapped_at_api_boundary(self):
        self.assertEqual(self.client.get("/api/v1/tasks/missing").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/tasks/..bad").status_code, 422)
        response = self.client.post("/api/v1/tasks/validate", json={"task_id": "bad"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("detail", response.json())
        self.assertEqual(self.client.post("/api/v1/tasks/validate", json=[]).status_code, 422)

    def test_repository_rejects_bad_file_and_path(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = JsonDocumentRepository(directory)
            self.assertEqual(repository.list_names(), [])
            with self.assertRaises(InvalidDocument):
                repository.get("../outside")
            (Path(directory) / "broken.json").write_text("[", encoding="utf-8")
            with self.assertRaises(InvalidDocument):
                repository.get("broken")

    def test_service_executes_with_existing_runtime(self):
        document = self.tasks.get("signal_analysis_task")
        samples = np.exp(2j * np.pi * 100_000 * np.arange(4096) / 4_000_000)
        packet = DataPacket(samples, 4_000_000, 100_000_000, 0, "time")
        result = self.tasks.execute(document, {"ddc-node": packet})
        self.assertTrue(result.full_task_completed)
        self.assertEqual(result.sink_outputs["sensing-node"].item_count, 1024)

    def test_resource_plan_and_simulated_instance_lifecycle(self):
        document = self.tasks.get("signal_analysis_task")
        plan = self.client.post("/api/v1/plans", json=document)
        self.assertEqual(plan.status_code, 200)
        self.assertEqual(plan.json()["status"], "FEASIBLE_ESTIMATE")
        self.assertEqual(plan.json()["total_demand"]["memory"], 35)

        first = self.client.post("/api/v1/simulations/schedule", json=document)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["reused"], [])
        self.assertEqual(self.client.post("/api/v1/simulations/schedule",
                                          json=document).status_code, 409)
        state = self.client.get("/api/v1/simulations/state").json()
        self.assertEqual(state["active_tasks"], [document["task_id"]])
        self.assertEqual({item["state"] for item in state["instances"]}, {"ACTIVE"})

        complete = self.client.post(
            f"/api/v1/simulations/tasks/{document['task_id']}/complete")
        self.assertEqual(complete.status_code, 200)
        self.assertEqual({item["state"] for item in complete.json()["instances"]}, {"WARM"})
        self.assertEqual(self.client.post("/api/v1/plans", json=document).json()["reused"],
                         list(first.json()["instance_ids"]))

    def test_scheduled_execution_releases_or_warms_instances(self):
        document = self.tasks.get("signal_analysis_task")
        with self.assertRaises(DAGExecutionError):
            self.tasks.execute_with_schedule(document, {})
        state = self.tasks.simulation_state()
        self.assertEqual(state["used"],
                         {"cpu": 0.0, "memory": 0.0, "bandwidth": 0.0})
        self.assertEqual({item["state"] for item in state["instances"]}, {"RELEASED"})

        samples = np.exp(2j * np.pi * 100_000 * np.arange(4096) / 4_000_000)
        packet = DataPacket(samples, 4_000_000, 100_000_000, 0, "time")
        decision, result = self.tasks.execute_with_schedule(
            document, {"ddc-node": packet})
        self.assertEqual(decision.plan.status, "FEASIBLE_ESTIMATE")
        self.assertTrue(result.full_task_completed)
        self.assertEqual({item["state"] for item in
                          self.tasks.simulation_state()["instances"]},
                         {"RELEASED", "WARM"})

    def test_deployment_endpoint_runs_matched_data_link_chain(self):
        from tests.test_deployment import SINK, SOURCE, LocalContainerRuntime, deployment_fixture

        runtime = LocalContainerRuntime()
        request, _, _, manager = deployment_fixture(runtime)
        tasks = TaskService(JsonDocumentRepository(ROOT / "examples" / "TaskDemos"),
                            deployment=manager)
        client = TestClient(create_app(tasks, self.resources))
        message = b"deployment endpoint"
        frames = [{SOURCE: encode_packet(aligned_iq_frame(message[:16], 8, 0, 2))},
                  {SOURCE: encode_packet(aligned_iq_frame(message[16:], 8, 1, 2))}]
        response = client.post("/api/v1/deployments/execute",
                               json={"task": request, "frames": frames})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "COMPLETED")
        self.assertEqual([node["cid"] for node in body["execution_graph"]["nodes"]],
                         ["C15", "C14", "C17", "C35", "C36"])
        self.assertEqual(len(body["frame_outputs"]), 2)
        self.assertEqual(body["frame_outputs"][1][SINK]["shape"], [len(message)])
        self.assertEqual(manager.scheduler.placement_snapshot()["active_tasks"], [])
        self.assertEqual(len(runtime.stopped), 5)

    def test_deployment_resources_separate_capacity_and_reservation(self):
        from tests.test_deployment import LocalContainerRuntime, deployment_fixture

        request, task, match, manager = deployment_fixture(LocalContainerRuntime())
        tasks = TaskService(JsonDocumentRepository(ROOT / "examples" / "TaskDemos"),
                            deployment=manager)
        client = TestClient(create_app(tasks, self.resources))
        idle = client.get("/api/v1/deployments/resources").json()
        self.assertEqual(idle["nodes"][0]["capacity"]["cpu_millicores"], 1000)
        self.assertEqual(idle["nodes"][0]["reserved"]["cpu_millicores"], 0)
        self.assertEqual(idle["containers"], [])
        manager.scheduler.submit_placement(task, match)
        active = client.get("/api/v1/deployments/resources").json()
        self.assertEqual(active["active_tasks"], [task.task_id])
        self.assertEqual(active["nodes"][0]["reserved"]["cpu_millicores"], 500)
        self.assertEqual(len(active["containers"]), 5)
        self.assertTrue(all(item["actual"] is None for item in active["containers"]))
        manager.scheduler.complete_placement(task.task_id)
        self.assertEqual(client.get("/api/v1/deployments/resources").json()["containers"], [])


if __name__ == "__main__":
    unittest.main()
