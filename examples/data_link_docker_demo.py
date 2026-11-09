"""使用已配置 Docker Engine 执行 500 字节数据链接收链。"""

import argparse
import json
from pathlib import Path

from backend.repositories import JsonDocumentRepository
from backend.services import TaskService
from deployment.config import load_deployment_manager
from examples.business_refinement_demo import configured_data_link_receive
from examples.data_link_receive_execution_demo import aligned_iq_frame


def main():
    parser = argparse.ArgumentParser(description="数据链接收容器部署演示")
    parser.add_argument("--config", required=True, help="本机 Docker 部署配置 JSON")
    args = parser.parse_args()
    manager = load_deployment_manager(args.config)
    service = TaskService(JsonDocumentRepository(Path(__file__).parent / "TaskDemos"),
                          deployment=manager)
    request = configured_data_link_receive()
    request["deadline_ms"] = request["arrival_ms"] + 30_000
    message = bytes(range(250)) * 2
    frames = ({"rx-02:ofdm_rx": aligned_iq_frame(message[offset:offset + 16],
                                                 1001, offset // 16, 32)}
              for offset in range(0, len(message), 16))
    result = service.execute_deployed(request, frames)
    received = result.frame_outputs[-1]["rx-02:reassemble"]
    print(json.dumps({"task_id": result.task_id, "status": "COMPLETED",
                      "plan": result.plan.to_dict(), "frame_count": len(result.frame_outputs),
                      "delivered_bytes": received.item_count,
                      "payload_equal": received.samples.tobytes() == message,
                      "remaining_reservations": manager.scheduler.placement_snapshot()["active_tasks"]},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
