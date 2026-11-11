"""Calibration control flow with real component computation and fake container metrics."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from components.implementations.runtime_registry import create_runtime_component
from components.runtime import DataPacket
from deployment.calibrate_local import calibrate
from deployment.docker_runtime import DockerCleanupError
from matching import match_task
from resources.measurements import load_measurements
from test_deployment import merge_task


class CalibrationRuntime:
    def __init__(self, *args, fail_process=False, fail_stop=False):
        self.components = {}
        self.stopped = []
        self.fail_process = fail_process
        self.fail_stop = fail_stop

    def start(self, allocation, node):
        self.components[node["id"]] = create_runtime_component(node["cid"], **node["parameters"])
        return SimpleNamespace(container_id=node["id"])

    def process(self, handle, packets):
        if self.fail_process:
            raise RuntimeError("processing failed")
        component = self.components[handle.container_id]
        return (component.process(next(iter(packets.values()))) if len(packets) == 1
                else component.process_inputs(packets))

    def stop(self, handle):
        self.stopped.append(handle.container_id)
        if self.fail_stop:
            raise RuntimeError("cleanup failed")


class CalibrationTests(unittest.TestCase):
    def run_calibration(self, directory, runtime, branched):
        graph = match_task(merge_task(branched)).execution_graph
        packet = DataPacket(np.ones(2, dtype=complex), 1000, 0, 0, "time")
        inputs = {"left": packet, "right": packet}
        frame = inputs if branched else {"join": inputs}
        def docker(*args):
            if args[0] == "info":
                return json.dumps(dict(ID="test", NCPU=8, MemTotal=4 * 1024**3, ServerVersion="fake"))
            return "sha256:test-image"
        with patch("deployment.calibrate_local.docker", side_effect=docker), \
             patch("deployment.calibrate_local.DockerRuntime", return_value=runtime), \
             patch("deployment.calibrate_local.cgroup_value", return_value=64 * 1024**2):
            return calibrate(directory, 20, execution_graph=graph, frames=[frame] * 20)

    def test_general_branch_and_multiport_calibration_records_observations(self):
        for branched in (True, False):
            with self.subTest(branched=branched), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                runtime = CalibrationRuntime()
                config = self.run_calibration(path, runtime, branched)
                self.assertTrue(config.exists())
                profiles = load_measurements(path / "measurements.local.json").profiles
                self.assertEqual(len(profiles), 3 if branched else 1)
                self.assertTrue(all(p.timing_scope == "docker_http_roundtrip" and
                                    p.setup_sample_count == 1 and p.sample_count == 20 for p in profiles))
                report = json.loads((path / "calibration.local.json").read_text(encoding="utf-8"))
                self.assertEqual(len(report["frame_ms"]), 20)
                self.assertEqual(report["session_sample_count"], 1)
                self.assertGreater(report["observed_session_ms"], 0)
                self.assertEqual(runtime.stopped, list(reversed(report["execution_graph"]["topological_order"])))

    def test_failed_process_and_failed_cleanup_attempt_every_container(self):
        for fail_stop in (False, True):
            with self.subTest(fail_stop=fail_stop), tempfile.TemporaryDirectory() as directory:
                runtime = CalibrationRuntime(fail_process=True, fail_stop=fail_stop)
                with self.assertRaises(DockerCleanupError if fail_stop else RuntimeError):
                    self.run_calibration(Path(directory), runtime, True)
                self.assertEqual(runtime.stopped, ["join", "right", "left"])
                self.assertFalse((Path(directory) / "measurements.local.json").exists())
