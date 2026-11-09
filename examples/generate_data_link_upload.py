"""生成工作台可直接加载的数据链接收请求与演示输入帧。"""

import argparse
import json
from pathlib import Path

from deployment.protocol import encode_packet
from examples.business_refinement_demo import configured_data_link_receive
from examples.data_link_receive_execution_demo import aligned_iq_frame
from task_repr import build_template, parse_external_task


def generate(output_dir: Path):
    task = configured_data_link_receive()
    task["deadline_ms"] = task["arrival_ms"] + 30_000
    normalized = parse_external_task(task)
    template = build_template([normalized]).to_dict()
    fragments = (b"0123456789abcdef", b"final")
    frames = [{"rx-02:ofdm_rx": encode_packet(aligned_iq_frame(fragment, 77, index, 2))}
              for index, fragment in enumerate(fragments)]
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, value in (("data-link-rx-task.json", task),
                        ("data-link-rx-frames.json", frames),
                        ("data-link-rx-representation.json", normalized),
                        ("data-link-rx-template.json", template)):
        (output_dir / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                                       encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="生成数据链接收上传样例")
    parser.add_argument("--output-dir", type=Path,
                        default=Path(__file__).resolve().parents[1] / "frontend" / "public" / "samples")
    generate(parser.parse_args().output_dir)


if __name__ == "__main__":
    main()
