"""演示外部数据链请求经过细化、匹配和 32 帧数据传递。"""

import json

from components.implementations.cni.data_link_component import encode_demo_frame
from components.implementations.cni.fec_component import FECComponent
from components.implementations.cni.ofdm_component import OFDMComponent
from components.implementations.cni.psk_qam_component import PSKQAMComponent
from components.runtime import DataPacket
from examples.business_refinement_demo import configured_data_link_receive
from matching import match_task
from task_executor import DAGExecutor
from task_repr import parse_external_task, refine_business_task


def aligned_iq_frame(payload: bytes, message_id: int, index: int, count: int) -> DataPacket:
    frame = encode_demo_frame(payload, message_id, index, count)
    packet = DataPacket(frame, 1_000_000, 0, index / 1000, "bits")
    packet = FECComponent(mode="encode", block_length=208, constraint_length=7,
                          generators=[121, 91]).process(packet)
    packet = PSKQAMComponent(mode="modulate", modulation="qpsk").process(packet)
    return OFDMComponent(mode="tx", fft_length=256, cp_length=32,
                         subcarrier_allocation=list(range(1, 215)),
                         pilot_pattern=[]).process(packet)


def main():
    refinement = refine_business_task(parse_external_task(configured_data_link_receive()))
    matched = match_task(refinement)
    if refinement.task is None or matched.status != "MATCHED":
        raise RuntimeError(matched.to_dict())
    graph = matched.execution_graph
    message = bytes(range(250)) * 2
    frames = ({graph["boundaries"][0]["node_id"]:
               aligned_iq_frame(message[offset:offset + 16], 1001, offset // 16, 32)}
              for offset in range(0, len(message), 16))
    results = DAGExecutor.from_match(refinement.task, matched).execute_stream(frames)
    received = results[-1].sink_outputs[graph["boundaries"][1]["node_id"]]
    print(json.dumps({
        "task_id": matched.task_id, "status": matched.status,
        "component_order": [{"node_id": node["id"], "cid": node["cid"]}
                            for node in graph["nodes"]],
        "connections": graph["edges"], "boundaries": graph["boundaries"],
        "frame_count": len(results), "delivered_bytes": received.item_count,
        "payload_equal": received.samples.tobytes() == message,
        "delivery": received.metadata["data_link_message"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
