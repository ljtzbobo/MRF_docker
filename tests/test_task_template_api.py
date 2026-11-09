"""验证五元组和公共任务模板上传后进入相同的匹配链。"""

from copy import deepcopy
from pathlib import Path
import unittest

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.repositories import JsonDocumentRepository
from backend.services import TaskService
from examples.business_refinement_demo import configured_data_link_receive
from task_repr import build_template, parse_external_task


ROOT = Path(__file__).resolve().parents[1]


class TaskTemplateApiTests(unittest.TestCase):
    def setUp(self):
        service = TaskService(JsonDocumentRepository(ROOT / "examples" / "TaskDemos"))
        self.client = TestClient(create_app(service))
        self.task = parse_external_task(configured_data_link_receive())

    def test_filled_five_part_template_can_be_represented_and_matched(self):
        response = self.client.post("/api/v1/task-representations/normalized", json=self.task)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["task_representation"], self.task)
        match = self.client.post("/api/v1/matches", json=self.task)
        self.assertEqual(match.status_code, 200)
        self.assertEqual(match.json()["status"], "MATCHED")
        self.assertEqual(len(match.json()["execution_graph"]["nodes"]), 5)

    def test_uploaded_public_template_restores_selected_variant(self):
        other = deepcopy(self.task)
        other["task_id"] = "RF-COM-DATALINK-SECOND"
        template = build_template([self.task, other]).to_dict()
        response = self.client.post("/api/v1/task-representations/from-template", json={
            "task_template": template, "task_id": other["task_id"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["task_representation"], other)

    def test_blank_or_malformed_five_part_template_is_rejected(self):
        blank = self.client.get("/api/v1/task-representations/template")
        self.assertEqual(blank.status_code, 200)
        self.assertEqual(blank.json()["schema_version"], "rf_task_representation.v1")
        self.assertEqual(self.client.post("/api/v1/task-representations/normalized",
                                          json=blank.json()).status_code, 422)
        invalid = deepcopy(self.task)
        invalid["G"]["edges"][0]["target"] = "missing-node"
        for path in ("/api/v1/task-representations/normalized", "/api/v1/matches", "/api/v1/plans"):
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, json=invalid).status_code, 422)


if __name__ == "__main__":
    unittest.main()
