"""验证数据链接收规格图从静态匹配到真实数据传递。"""

from dataclasses import replace
import unittest

import numpy as np

from components.implementations.cni.data_link_component import (
    DataLinkFrameVerifyComponent, DataLinkReassemblyComponent, encode_demo_frame)
from components.implementations.cni.fec_component import FECComponent
from components.implementations.cni.ofdm_component import OFDMComponent
from components.implementations.cni.psk_qam_component import PSKQAMComponent
from components.runtime import DataPacket
from examples.business_refinement_demo import configured_data_link_receive
from matching import match_task
from task_executor import DAGExecutionError, DAGExecutor
from task_repr import parse_external_task, refine_business_task
from task_repr.template.refinement import (SpecificationEdge, SpecificationGraph,
                                          SpecificationNode, SpecificationSemantics,
                                          SpecificationTask)


ROOT = "rx-02:ofdm_rx"
SINK = "rx-02:reassemble"


def signal_frame(payload, message_id, index, count):
    bits = encode_demo_frame(payload, message_id, index, count)
    packet = DataPacket(bits, 1_000_000, 0, index / 1000, "bits")
    packet = FECComponent(mode="encode", block_length=208, constraint_length=7,
                          generators=[121, 91]).process(packet)
    packet = PSKQAMComponent(mode="modulate", modulation="qpsk").process(packet)
    return OFDMComponent(mode="tx", fft_length=256, cp_length=32,
                         subcarrier_allocation=list(range(1, 215)),
                         pilot_pattern=[]).process(packet)


class SpecificationExecutionTests(unittest.TestCase):
    def setUp(self):
        request = configured_data_link_receive()
        refinement = refine_business_task(parse_external_task(request))
        self.assertIsNotNone(refinement.task)
        self.task = refinement.task
        self.match = match_task(refinement, max_solutions=2)
        self.assertEqual(self.match.status, "MATCHED", self.match.to_dict())

    def test_execution_graph_contains_ports_boundaries_and_order(self):
        graph = self.match.execution_graph
        self.assertEqual([node["cid"] for node in graph["nodes"]],
                         ["C15", "C14", "C17", "C35", "C36"])
        self.assertEqual(len(graph["edges"]), 4)
        self.assertEqual(graph["edges"][0]["source_port"], "out")
        self.assertEqual(graph["edges"][0]["target_port"], "in")
        self.assertEqual(graph["boundaries"][0]["node_id"], ROOT)
        self.assertEqual(graph["boundaries"][1]["node_id"], SINK)
        self.assertEqual(graph["topological_order"], [node["id"] for node in graph["nodes"]])
        self.assertEqual(len(self.match.candidate_execution_graphs), 1)
        self.assertEqual(self.match.to_dict()["execution_graph"], graph)

    def test_out_of_order_fragments_deliver_original_message(self):
        executor = DAGExecutor.from_match(self.task, self.match)
        message = bytes(range(37))
        frames = [{ROOT: signal_frame(message[i:i + 16], 42, i // 16, 3)}
                  for i in (16, 0, 32)]
        results = executor.execute_stream(frames)
        self.assertEqual([result.sink_outputs[SINK].item_count for result in results], [0, 0, 37])
        self.assertEqual(results[-1].sink_outputs[SINK].samples.tobytes(), message)
        self.assertEqual(results[-1].sink_outputs[SINK].metadata["data_link_message"]["status"],
                         "delivered")
        self.assertTrue(all(result.full_task_completed for result in results))

    def test_execution_graph_runs_without_original_task(self):
        executor = DAGExecutor.from_execution_graph(self.match.execution_graph)
        result = executor.execute({ROOT: signal_frame(b"independent", 45, 0, 1)})
        self.assertEqual(result.sink_outputs[SINK].samples.tobytes(), b"independent")

    def test_missing_fragment_stays_pending(self):
        result = DAGExecutor.from_match(self.task, self.match).execute_stream(
            [{ROOT: signal_frame(b"0123456789abcdef", 7, 0, 2)}])[0]
        self.assertEqual(result.sink_outputs[SINK].item_count, 0)
        self.assertEqual(result.sink_outputs[SINK].metadata["data_link_message"]["status"],
                         "pending")

    def test_configured_500_byte_message_crosses_32_frames(self):
        message = bytes(range(250)) * 2
        frames = [{ROOT: signal_frame(message[offset:offset + 16], 1001,
                                      offset // 16, 32)}
                  for offset in range(0, len(message), 16)]
        results = DAGExecutor.from_match(self.task, self.match).execute_stream(frames)
        self.assertEqual(len(results), 32)
        self.assertTrue(all(result.sink_outputs[SINK].item_count == 0
                            for result in results[:-1]))
        self.assertEqual(results[-1].sink_outputs[SINK].samples.tobytes(), message)

    def test_crc_error_rejects_frame(self):
        # 直接验证 CRC 节点，以免把信道错误纠正能力误认为 CRC 功能。
        bits = encode_demo_frame(b"valid", 9, 0, 1).copy()
        bits[80] ^= 1
        with self.assertRaisesRegex(ValueError, "CRC16"):
            DataLinkFrameVerifyComponent().process(DataPacket(bits, 1_000_000, 0, 0, "bits"))

    def test_reassembly_reset_discards_pending_fragments(self):
        verifier = DataLinkFrameVerifyComponent()
        reassembler = DataLinkReassemblyComponent()
        first = verifier.process(DataPacket(encode_demo_frame(b"0123456789abcdef", 11, 0, 2),
                                            1_000_000, 0, 0, "bits"))
        second = verifier.process(DataPacket(encode_demo_frame(b"tail", 11, 1, 2),
                                             1_000_000, 0, 0.001, "bits"))
        self.assertEqual(reassembler.process(first).item_count, 0)
        self.assertEqual(reassembler.reset_buffer(), 16)
        self.assertEqual(reassembler.process(second).item_count, 0)

    def test_tampered_execution_graph_is_rejected(self):
        graph = dict(self.match.execution_graph)
        graph["edges"] = []
        with self.assertRaisesRegex(DAGExecutionError, "execution graph differs"):
            DAGExecutor.from_match(self.task, replace(self.match, execution_graph=graph))
        graph = dict(self.match.execution_graph)
        graph["nodes"] = [dict(node) for node in graph["nodes"]]
        graph["nodes"][0]["cid"] = "C05"
        with self.assertRaisesRegex(DAGExecutionError, "does not implement"):
            DAGExecutor.from_execution_graph(graph)

    def test_branch_join_waits_for_both_input_ports(self):
        nodes = (
            SpecificationNode("left", "left", "fir_filter", {"in": "iq/complex"},
                              {"out": "iq/complex"}, {"coefficients": "[1]"}, "left"),
            SpecificationNode("right", "right", "fir_filter", {"in": "iq/complex"},
                              {"out": "iq/complex"}, {"coefficients": "[1]"}, "right"),
            SpecificationNode("join", "join", "iq_branch_merge",
                              {"left": "iq/complex", "right": "iq/complex"},
                              {"out": "iq/complex"}, {"gain_left": 1, "gain_right": 1}, "join"),
        )
        boundaries = ({"direction": "source", "node_id": "left", "port": "in",
                       "format": "iq/complex", "business_node_id": "left"},
                      {"direction": "source", "node_id": "right", "port": "in",
                       "format": "iq/complex", "business_node_id": "right"},
                      {"direction": "sink", "node_id": "join", "port": "out",
                       "format": "iq/complex", "business_node_id": "join"})
        graph = SpecificationGraph(nodes, (
            SpecificationEdge("left", "out", "join", "left"),
            SpecificationEdge("right", "out", "join", "right")),
            boundaries, "branch_join_test", "1.0", "component_dag", {}, (), (), ())
        task = SpecificationTask("branch-join", SpecificationSemantics(
            "test", "test", "rx", {}), {}, {}, graph, {})
        matched = match_task(task)
        self.assertEqual(matched.status, "MATCHED", matched.to_dict())
        self.assertEqual(matched.execution_graph["topological_order"],
                         ["left", "right", "join"])
        self.assertEqual([edge["target_port"] for edge in matched.execution_graph["edges"]],
                         ["left", "right"])
        executor = DAGExecutor.from_match(task, matched)
        left = DataPacket(np.array([1 + 1j, 2 + 2j]), 1000, 0, 0, "time")
        right = DataPacket(np.array([3 + 3j, 4 + 4j]), 1000, 0, 0, "time")
        result = executor.execute({"left": left, "right": right})
        np.testing.assert_allclose(result.sink_outputs["join"].samples,
                                   [4 + 4j, 6 + 6j])
        self.assertTrue(result.full_task_completed)
        standalone = DAGExecutor.from_execution_graph(matched.execution_graph)
        np.testing.assert_allclose(standalone.execute({"left": left, "right": right})
                                   .sink_outputs["join"].samples, [4 + 4j, 6 + 6j])

        missing = replace(task, G=replace(graph, edges=graph.edges[:1]))
        partial = match_task(missing)
        self.assertEqual(partial.status, "PARTIAL")
        self.assertTrue(any(issue.code == "INCOMPLETE_DAG" for issue in partial.unresolved))
        cyclic = replace(task, G=replace(graph, edges=graph.edges + (
            SpecificationEdge("join", "out", "left", "in"),)))
        result = match_task(cyclic)
        self.assertEqual(result.status, "PARTIAL")
        self.assertTrue(any("cycle" in issue.detail for issue in result.unresolved))


if __name__ == "__main__":
    unittest.main()
