"""新规格任务的容器部署事务与 HTTP 数据包接口验收。"""

from http.server import ThreadingHTTPServer
from dataclasses import replace
import json
import os
from pathlib import Path
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import numpy as np

from backend.repositories import JsonDocumentRepository
from backend.services import TaskService
from components.implementations.cni.data_link_component import encode_demo_frame
from components.implementations.runtime_registry import create_runtime_component
from components.runtime import DataPacket
from deployment import DeploymentError, DeploymentManager
from deployment.docker_runtime import DockerRuntime, DockerRuntimeError
from deployment.protocol import decode_packet, encode_packet
from deployment.worker import make_handler
from examples.business_refinement_demo import configured_data_link_receive
from examples.data_link_receive_execution_demo import aligned_iq_frame
from matching import match_task
from resources import (ComponentMeasurement, MeasurementCatalog,
                       PhysicalHardwareProfile, PhysicalUsage)
from resources.placement import (EdgeTransfer, PlacementPlanner, ProcessingNode)
from scheduler import TaskScheduler
from task_executor import DAGExecutionError, DAGExecutor
from task_repr import parse_external_task, refine_business_task
from task_repr.template.refinement import (SpecificationEdge, SpecificationGraph,
                                          SpecificationNode, SpecificationSemantics,
                                          SpecificationTask)


ROOT = Path(__file__).resolve().parents[1]
SOURCE = "rx-02:ofdm_rx"
SINK = "rx-02:reassemble"


class LocalContainerRuntime:
    """测试适配器保留独立组件实例，并强制穿过容器数据包编码。"""

    def __init__(self, fail_start=None, fail_process=None):
        self.fail_start = fail_start
        self.fail_process = fail_process
        self.instances = {}
        self.started = []
        self.stopped = []

    def preflight(self, allocations):
        if any(item.processor != "cpu" for item in allocations):
            raise ValueError("test runtime supports CPU only")

    def start(self, allocation, node):
        if node["cid"] == self.fail_start:
            raise RuntimeError("injected container start failure")
        handle = allocation.container_unit_id
        self.instances[handle] = create_runtime_component(node["cid"], **node["parameters"])
        self.started.append(handle)
        return handle

    def process(self, handle, packets):
        component = self.instances[handle]
        if component.cid == self.fail_process:
            raise RuntimeError("injected container process failure")
        decoded = {port: decode_packet(encode_packet(packet)) for port, packet in packets.items()}
        output = (component.process(next(iter(decoded.values()))) if len(decoded) == 1 else
                  component.process_inputs(decoded))
        return decode_packet(encode_packet(output))

    def stop(self, handle):
        self.instances.pop(handle)
        self.stopped.append(handle)


class HttpWorkerRuntime:
    """测试中以独立 HTTP 服务代替 Docker 启动，使用同一工作进程协议。"""

    def __init__(self):
        self.servers = {}

    def preflight(self, allocations):
        return None

    def start(self, allocation, node):
        component = create_runtime_component(node["cid"], **node["parameters"])
        specification = SpecificationNode(node["id"], node["id"], "worker",
                                          node["inputs"], node["outputs"], {}, node["id"])
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(component, specification))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        handle = allocation.container_unit_id
        self.servers[handle] = (server, thread)
        return handle

    def process(self, handle, packets):
        server, _ = self.servers[handle]
        body = json.dumps({"inputs": {port: encode_packet(packet)
                                      for port, packet in packets.items()}}).encode()
        request = Request(f"http://127.0.0.1:{server.server_port}/process", body,
                          {"Content-Type": "application/json"})
        with urlopen(request, timeout=3) as response:
            return decode_packet(json.load(response)["output"])

    def stop(self, handle):
        server, thread = self.servers.pop(handle)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def deployment_fixture(runtime, task=None):
    request = configured_data_link_receive()
    request["deadline_ms"] = 1500
    if task is None:
        task = refine_business_task(parse_external_task(request)).task
    match = match_task(task)
    sizes = {"C15": (4608, 3424), "C14": (3424, 3424),
             "C17": (3424, 208), "C35": (208, 16), "C36": (16, 500),
             "C05": (2, 2), "C37": (2, 2)}
    profiles = tuple(ComponentMeasurement(
        binding.cid, f"{binding.cid}-local", "cpu", "test-hardware", (),
        binding.parameters, PhysicalUsage(100, 128 * 1024 * 1024, 1000000, 0, 0),
        1, 0, *sizes[binding.cid], 20, "test-fixture-only")
        for binding in match.bindings.values())
    transfers = tuple(EdgeTransfer(edge.source, edge.target,
                                   sizes[match.bindings[edge.source].cid][1], 1_000_000)
                      for edge in task.G.edges)
    hardware = PhysicalHardwareProfile("test-hardware", PhysicalUsage(
        1000, 1024 * 1024 * 1024, 10_000_000, 0, 0))
    planner = PlacementPlanner((ProcessingNode("local-docker", hardware),), (),
                               MeasurementCatalog(profiles), transfers)
    scheduler = TaskScheduler(placement_planner=planner)
    manager = DeploymentManager(scheduler, runtime)
    return request, task, match, manager


def merge_task(branched=True):
    merge = SpecificationNode("join", "join", "iq_branch_merge",
                              {"left": "iq/complex", "right": "iq/complex"},
                              {"out": "iq/complex"},
                              {"gain_left": 2, "gain_right": 3, "sample_rate_mhz": 0.001,
                               "input_block_length": 2, "output_block_length": 2}, "join")
    nodes = tuple(SpecificationNode(name, name, "fir_filter", {"in": "iq/complex"},
                                    {"out": "iq/complex"}, {"coefficients": "[1]"}, name)
                  for name in ("left", "right")) if branched else ()
    edges = tuple(SpecificationEdge(name, "out", "join", name)
                  for name in ("left", "right")) if branched else ()
    boundaries = tuple({"direction": "source", "node_id": name if branched else "join",
                        "port": "in" if branched else name, "format": "iq/complex",
                        "business_node_id": name if branched else "join"}
                       for name in ("left", "right")) + (
        {"direction": "sink", "node_id": "join", "port": "out",
         "format": "iq/complex", "business_node_id": "join"},)
    graph = SpecificationGraph(nodes + (merge,), edges, boundaries,
                               "merge-test", "1.0", "component_dag", {}, (), (), ())
    return SpecificationTask("merge-test", SpecificationSemantics("test", "test", "rx", {}),
                             {"arrival_ms": 0, "deadline_ms": 1000}, {}, graph,
                             {"observed_at_ms": 0})


class DeploymentTests(unittest.TestCase):
    def test_deployment_uses_selected_algorithm_and_actual_frame_count(self):
        frames = [{SOURCE: aligned_iq_frame(b"0123456789abcdef", 99, 0, 2)},
                  {SOURCE: aligned_iq_frame(b"tail", 99, 1, 2)}]
        for algorithm in ("chain_dp", "heft"):
            with self.subTest(algorithm=algorithm):
                runtime = LocalContainerRuntime()
                _, task, match, manager = deployment_fixture(runtime)
                result = manager.execute(task, match, iter(frames), algorithm=algorithm, frame_count=2)
                self.assertEqual(result.plan.algorithm, algorithm)
                self.assertEqual(result.plan.frame_count, 2)
                self.assertEqual(result.plan.estimated_completion_ms, 10)
                self.assertEqual(result.frame_outputs[-1][SINK].samples.tobytes(), b"0123456789abcdeftail")
                self.assertEqual(runtime.stopped, list(reversed(runtime.started)))

    def test_multiframe_deadline_rejection_happens_before_container_start(self):
        runtime = LocalContainerRuntime()
        _, task, match, manager = deployment_fixture(runtime)
        task = replace(task, X={**task.X, "remaining_deadline_ms": 5})
        frame = {SOURCE: aligned_iq_frame(b"hello", 1, 0, 1)}
        with self.assertRaisesRegex(DeploymentError, "INFEASIBLE"):
            manager.execute(task, match, [frame, frame])
        self.assertFalse(runtime.started)
        self.assertEqual(manager.scheduler.placement_snapshot()["active_tasks"], [])

    def test_stream_frame_count_mismatch_releases_containers(self):
        for supplied, expected in ((1, 2), (2, 1)):
            with self.subTest(supplied=supplied, expected=expected):
                runtime = LocalContainerRuntime()
                _, task, match, manager = deployment_fixture(runtime)
                frame = {SOURCE: aligned_iq_frame(b"hello", 1, 0, 1)}
                with self.assertRaisesRegex(DeploymentError, "frame count differs"):
                    manager.execute(task, match, iter([frame] * supplied), frame_count=expected)
                self.assertEqual(runtime.stopped, list(reversed(runtime.started)))
                self.assertEqual(manager.scheduler.placement_snapshot()["active_tasks"], [])

    def assert_packet_equal(self, left, right):
        np.testing.assert_array_equal(left.samples, right.samples)
        self.assertEqual(left.samples.dtype, right.samples.dtype)
        for field in ("sample_rate", "center_frequency", "timestamp", "domain", "metadata"):
            self.assertEqual(getattr(left, field), getattr(right, field))

    def test_local_and_container_streams_are_equivalent(self):
        runtime = LocalContainerRuntime()
        _, task, match, manager = deployment_fixture(runtime)
        frames = [{SOURCE: aligned_iq_frame(payload, 91, index, 3)} for index, payload in
                  ((1, b"abcdefghijklmnop"), (0, b"0123456789abcdef"), (2, b"tail"))]
        local = DAGExecutor.from_match(task, match).execute_stream(frames)
        # Docker 的本地对象只用于构造时校验，不能随帧重新创建。
        with patch("task_executor.executor.create_runtime_component",
                   wraps=create_runtime_component) as create:
            deployed = manager.execute(task, match, iter(frames))
        self.assertEqual(create.call_count, len(match.bindings))
        self.assertEqual([frame[SINK].item_count for frame in deployed.frame_outputs], [0, 0, 36])
        for expected, actual in zip(local, deployed.frame_outputs):
            self.assert_packet_equal(expected.sink_outputs[SINK], actual[SINK])
        self.assertEqual(len(runtime.started), 5)
        self.assertEqual(runtime.stopped, list(reversed(runtime.started)))

    def test_branch_join_and_multiport_root_are_equivalent(self):
        left = DataPacket(np.array([1 + 1j, 2 + 2j]), 1000, 0, 0, "time")
        right = DataPacket(np.array([3 + 3j, 4 + 4j]), 1000, 0, 0, "time")
        for branched in (True, False):
            with self.subTest(branched=branched):
                runtime = LocalContainerRuntime()
                _, task, match, manager = deployment_fixture(runtime, merge_task(branched))
                inputs = {"left": left, "right": right}
                frame = inputs if branched else {"join": inputs}
                local = DAGExecutor.from_match(task, match).execute(frame)
                deployed = manager.execute(task, match, [frame])
                self.assert_packet_equal(local.sink_outputs["join"], deployed.frame_outputs[0]["join"])
                np.testing.assert_array_equal(deployed.frame_outputs[0]["join"].samples,
                                              2 * left.samples + 3 * right.samples)
                self.assertEqual(runtime.stopped, list(reversed(runtime.started)))

    def test_invalid_frames_match_local_errors_and_release_resources(self):
        good = aligned_iq_frame(b"hello", 1, 0, 1)
        bad_domain = replace(good, domain="frequency")
        bad_dtype = replace(good, samples=good.samples.real)
        bad_length = replace(good, samples=good.samples[:10])
        for frame in ({}, {SOURCE: good, "unknown": good}, {SOURCE: {"wrong": good}},
                      {SOURCE: bad_domain}, {SOURCE: bad_dtype}, {SOURCE: bad_length}):
            with self.subTest(frame=list(frame)):
                runtime = LocalContainerRuntime()
                _, task, match, manager = deployment_fixture(runtime)
                with self.assertRaises(DAGExecutionError) as local:
                    DAGExecutor.from_match(task, match).execute(frame)
                with self.assertRaises(DeploymentError) as deployed:
                    manager.execute(task, match, [frame])
                self.assertEqual(str(local.exception), str(deployed.exception))
                self.assertEqual(runtime.stopped, list(reversed(runtime.started)))
                self.assertEqual(manager.scheduler.placement_snapshot()["active_tasks"], [])

    def test_container_output_is_validated_before_downstream_invocation(self):
        class BadOutputRuntime(LocalContainerRuntime):
            def process(self, handle, packets):
                output = super().process(handle, packets)
                if self.instances[handle].cid == "C15":
                    return replace(output, domain="bits")
                raise AssertionError("invalid output reached downstream node")

        runtime = BadOutputRuntime()
        _, task, match, manager = deployment_fixture(runtime)
        with self.assertRaisesRegex(DeploymentError, f"node {SOURCE} output: .*domain"):
            manager.execute(task, match, [{SOURCE: aligned_iq_frame(b"hello", 1, 0, 1)}])
        self.assertEqual(runtime.stopped, list(reversed(runtime.started)))
        self.assertEqual(manager.scheduler.placement_snapshot()["active_tasks"], [])

    def test_container_path_preserves_runtime_port_dimension_constraint(self):
        runtime = LocalContainerRuntime()
        _, task, match, manager = deployment_fixture(runtime, merge_task(False))
        packet = DataPacket(np.ones((1, 2), dtype=complex), 1000, 0, 0, "time")
        # iq/complex 的 OPS 允许二维，C37 的实际端口只允许一维。
        with patch.object(runtime, "process") as invoke:
            with self.assertRaisesRegex(DeploymentError, "join input: .*dimensions"):
                manager.execute(task, match, [{"join": {"left": packet, "right": packet}}])
        invoke.assert_not_called()
        self.assertEqual(runtime.stopped, list(reversed(runtime.started)))

    def test_signal_constraints_are_shared_with_container_adapter(self):
        good = DataPacket(np.ones(2, dtype=complex), 1000, 0, 0, "time")
        for bad, detail in ((replace(good, sample_rate=2000), "sample rate"),
                            (replace(good, samples=good.samples[:1]), "block length")):
            with self.subTest(detail=detail):
                runtime = LocalContainerRuntime()
                _, task, match, manager = deployment_fixture(runtime, merge_task(False))
                frame = {"join": {"left": bad, "right": good}}
                with self.assertRaisesRegex(DAGExecutionError, detail) as local:
                    DAGExecutor.from_match(task, match).execute(frame)
                with self.assertRaisesRegex(DeploymentError, detail) as deployed:
                    manager.execute(task, match, [frame])
                self.assertEqual(str(local.exception), str(deployed.exception))
                self.assertEqual(manager.scheduler.placement_snapshot()["active_tasks"], [])

    def test_all_five_workers_transfer_over_http(self):
        runtime = HttpWorkerRuntime()
        request, task, match, manager = deployment_fixture(runtime)
        service = TaskService(JsonDocumentRepository(ROOT / "examples" / "TaskDemos"),
                              deployment=manager)
        result = service.execute_deployed(request, [
            {SOURCE: aligned_iq_frame(b"0123456789abcdef", 77, 0, 2)},
            {SOURCE: aligned_iq_frame(b"final", 77, 1, 2)},
        ])
        self.assertEqual(result.frame_outputs[0][SINK].item_count, 0)
        self.assertEqual(result.frame_outputs[1][SINK].samples.tobytes(),
                         b"0123456789abcdeffinal")
        self.assertFalse(runtime.servers)
        self.assertEqual(manager.scheduler.placement_snapshot()["active_tasks"], [])

    def test_service_runs_500_byte_message_and_releases_everything(self):
        runtime = LocalContainerRuntime()
        request, task, match, manager = deployment_fixture(runtime)
        service = TaskService(JsonDocumentRepository(ROOT / "examples" / "TaskDemos"),
                              deployment=manager)
        self.assertEqual(service.plan(request)["status"], "FEASIBLE_ESTIMATE")
        self.assertEqual(manager.scheduler.placement_snapshot()["active_tasks"], [])
        message = bytes(range(250)) * 2
        frames = [{SOURCE: aligned_iq_frame(message[offset:offset + 16], 1001,
                                            offset // 16, 32)}
                  for offset in range(0, len(message), 16)]
        result = service.execute_deployed(request, frames)
        self.assertEqual(result.plan.status, "FEASIBLE_ESTIMATE")
        self.assertEqual(result.frame_outputs[-1][SINK].samples.tobytes(), message)
        self.assertTrue(all(item[SINK].item_count == 0 for item in result.frame_outputs[:-1]))
        self.assertEqual(len(runtime.started), 5)
        self.assertEqual(runtime.stopped, list(reversed(runtime.started)))
        state = manager.scheduler.placement_snapshot()
        self.assertEqual(state["active_tasks"], [])
        self.assertEqual(state["node_used"]["local-docker"]["cpu_millicores"], 0)
        self.assertEqual(state["node_used"]["local-docker"]["memory_bytes"], 0)

    def test_failed_start_and_process_release_containers_and_capacity(self):
        for runtime, error in ((LocalContainerRuntime(fail_start="C17"), "start failure"),
                               (LocalContainerRuntime(fail_process="C35"), "process failure")):
            with self.subTest(error=error):
                request, task, match, manager = deployment_fixture(runtime)
                service = TaskService(JsonDocumentRepository(ROOT / "examples" / "TaskDemos"),
                                      deployment=manager)
                with self.assertRaisesRegex(RuntimeError, error):
                    service.execute_deployed(request, [{SOURCE: aligned_iq_frame(b"hello", 1, 0, 1)}])
                self.assertEqual(len(runtime.instances), 0)
                self.assertEqual(len(runtime.stopped), len(runtime.started))
                self.assertEqual(manager.scheduler.placement_snapshot()["active_tasks"], [])
                self.assertEqual(manager.scheduler.placement_snapshot()["node_used"]
                                 ["local-docker"]["memory_bytes"], 0)

    def test_unmatched_and_missing_measurements_do_not_start_containers(self):
        runtime = LocalContainerRuntime()
        request, task, match, manager = deployment_fixture(runtime)
        request["expected_output"].append("link_delivery_statistics")
        service = TaskService(JsonDocumentRepository(ROOT / "examples" / "TaskDemos"),
                              deployment=manager)
        with self.assertRaisesRegex(DeploymentError, "not fully matched"):
            service.execute_deployed(request, [{SOURCE: aligned_iq_frame(b"hello", 1, 0, 1)}])
        manager.scheduler.placement_planner.measurements = MeasurementCatalog()
        request["expected_output"].pop()
        with self.assertRaisesRegex(DeploymentError, "NEEDS_CALIBRATION"):
            service.execute_deployed(request, [{SOURCE: aligned_iq_frame(b"hello", 1, 0, 1)}])
        self.assertFalse(runtime.started)
        self.assertEqual(manager.scheduler.placement_snapshot()["active_tasks"], [])

    def test_failed_container_removal_keeps_capacity_reserved(self):
        class FailedDockerRuntime(DockerRuntime):
            def preflight(self, allocations):
                return None

            def _docker(self, command, *args):
                if command == "run":
                    return "container-123"
                raise DockerRuntimeError(f"docker {command} failed")

        runtime = FailedDockerRuntime("local-docker", {cid: "test-image" for cid in
                                      ("C15", "C14", "C17", "C35", "C36")})
        request, task, match, manager = deployment_fixture(runtime)
        service = TaskService(JsonDocumentRepository(ROOT / "examples" / "TaskDemos"),
                              deployment=manager)
        with self.assertRaisesRegex(DeploymentError, "placement remains reserved"):
            service.execute_deployed(request, [{SOURCE: aligned_iq_frame(b"hello", 1, 0, 1)}])
        state = manager.scheduler.placement_snapshot()
        self.assertEqual(state["active_tasks"], [task.task_id])
        self.assertGreater(state["node_used"]["local-docker"]["memory_bytes"], 0)

    def test_docker_runtime_rejects_non_cpu_allocations(self):
        request, task, match, manager = deployment_fixture(LocalContainerRuntime())
        allocation = manager.scheduler.preview_placement(task, match).allocations[0]
        runtime = DockerRuntime("local-docker", {allocation.cid: "test-image"})
        with patch.object(runtime, "_docker", return_value="test-server"):
            with self.assertRaisesRegex(DockerRuntimeError, "supports CPU only"):
                runtime.preflight((replace(allocation, processor="dsp"),))
        with self.assertRaisesRegex(DockerRuntimeError, "allocation and component graph disagree"):
            runtime.start(replace(allocation, processor="dsp"), {"cid": allocation.cid})

    def test_docker_resource_snapshot_keeps_stats_separate(self):
        from deployment.docker_runtime import ContainerHandle

        runtime = DockerRuntime("local-docker", {"C15": "test-image"})
        handle = ContainerHandle("container-123", "http://127.0.0.1:8000", "task:node")
        runtime._handles[handle.unit_id] = handle
        raw = json.dumps({"CPUPerc": "3.5%", "MemUsage": "40MiB / 128MiB",
                          "NetIO": "2kB / 4kB"})
        with patch.object(runtime, "_docker", return_value=raw):
            self.assertEqual(runtime.snapshot()[handle.unit_id]["cpu_percent"], "3.5%")
        with patch.object(runtime, "_docker", side_effect=DockerRuntimeError("offline")):
            self.assertEqual(runtime.snapshot()[handle.unit_id]["status"], "UNAVAILABLE")

    @unittest.skipUnless(os.environ.get("MRF_TEST_DOCKER") == "1", "requires local Docker image and Engine")
    def test_real_docker_data_link_chain(self):
        runtime = DockerRuntime("local-docker", {cid: "mrf-component:local" for cid in
                                                 ("C15", "C14", "C17", "C35", "C36")})
        request, _, _, manager = deployment_fixture(runtime)
        request["deadline_ms"] = request["arrival_ms"] + 60_000
        service = TaskService(JsonDocumentRepository(ROOT / "examples" / "TaskDemos"),
                              deployment=manager)
        message = bytes(range(250)) * 2
        frames = ({SOURCE: aligned_iq_frame(message[offset:offset + 16], 1001,
                                            offset // 16, 32)}
                  for offset in range(0, len(message), 16))
        result = service.execute_deployed(request, frames)
        self.assertEqual(result.frame_outputs[-1][SINK].samples.tobytes(), message)
        self.assertEqual(manager.scheduler.placement_snapshot()["active_tasks"], [])

    def test_worker_http_contract_and_crc_rejection(self):
        component = create_runtime_component("C35", frame_format="demo_crc_frame_v1",
                                             crc="crc16", payload_bits=128,
                                             header_bits=64, crc_bits=16)
        node = SpecificationNode("verify", "verify", "data_link_frame_crc_verify",
                                 {"in": "data/decoded_bits"}, {"out": "data/fragment"}, {}, "verify")
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(component, node))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            address = f"http://127.0.0.1:{server.server_port}"
            frame = encode_demo_frame(b"hello", 1, 0, 1)
            packet = DataPacket(frame, 1_000_000, 0, 0, "bits")
            def send(value):
                body = json.dumps({"inputs": {"in": encode_packet(value)}}).encode()
                return urlopen(Request(address + "/process", body,
                                       {"Content-Type": "application/json"}), timeout=3)
            with send(packet) as response:
                output = decode_packet(json.load(response)["output"])
            self.assertEqual(output.samples.tobytes(), b"hello")
            broken = frame.copy()
            broken[80] ^= 1
            with self.assertRaises(HTTPError) as captured:
                send(DataPacket(broken, 1_000_000, 0, 0, "bits"))
            self.assertEqual(captured.exception.code, 422)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
