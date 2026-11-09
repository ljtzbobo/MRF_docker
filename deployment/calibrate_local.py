"""实测本机 Docker 数据链接收链并生成专用部署画像。"""

from dataclasses import asdict
import argparse
import json
import math
from pathlib import Path
import subprocess
import time

from deployment.docker_runtime import DockerRuntime
from examples.business_refinement_demo import configured_data_link_receive
from examples.data_link_receive_execution_demo import aligned_iq_frame
from matching import match_task
from resources.measurements import PhysicalUsage
from resources.placement import PlacementAllocation


SOURCE = "rx-02:ofdm_rx"
SINK = "rx-02:reassemble"
IMAGE = "mrf-component:local"


def docker(*args):
    result = subprocess.run(["docker", *args], capture_output=True, text=True,
                            timeout=30, check=True)
    return result.stdout.strip()


def cgroup_value(container_id, file_name, key=None):
    raw = docker("exec", container_id, "cat", "/sys/fs/cgroup/" + file_name)
    if key is None:
        return int(raw)
    return int(next(line.split()[1] for line in raw.splitlines()
                    if line.split()[0] == key))


def p95(values):
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * 0.95) - 1]


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def calibrate(output_dir: Path, frames_count: int):
    if frames_count < 20:
        raise ValueError("至少需要 20 帧来观察 p95 处理时间")
    info = json.loads(docker("info", "--format", "{{json .}}"))
    if not info.get("ID") or not info.get("NCPU") or not info.get("MemTotal"):
        raise RuntimeError("无法读取 Docker Engine 的硬件容量")
    image_id = docker("image", "inspect", IMAGE, "--format", "{{.Id}}")
    match = match_task(configured_data_link_receive())
    if match.status != "MATCHED" or not match.execution_graph:
        raise RuntimeError("数据链接收任务未完整静态匹配")
    graph = match.execution_graph
    nodes = {node["id"]: node for node in graph["nodes"]}
    runtime = DockerRuntime("local-docker", {node["cid"]: IMAGE for node in nodes.values()})
    handles = {}
    start_ms = {}
    measurements = {node_id: {"runtime_ms": [], "input_bytes": [], "output_bytes": []}
                    for node_id in nodes}
    message = bytes(index % 256 for index in range(frames_count * 16))
    cpu_before = {}
    wall_before = time.perf_counter()
    try:
        for node_id in graph["topological_order"]:
            node = nodes[node_id]
            allocation = PlacementAllocation(node_id, node["cid"],
                f"{node['cid']}-docker-{image_id[7:19]}",
                f"calibration:{node_id}", "local-docker", "cpu",
                PhysicalUsage(1000, 256 * 1024 * 1024, 0, 0, 0), 1, 0)
            started = time.perf_counter()
            handles[node_id] = runtime.start(allocation, node)
            start_ms[node_id] = (time.perf_counter() - started) * 1000
        cpu_before = {node_id: cgroup_value(handle.container_id, "cpu.stat", "usage_usec")
                      for node_id, handle in handles.items()}
        wall_before = time.perf_counter()
        outputs = []
        for index in range(frames_count):
            produced = {}
            for node_id in graph["topological_order"]:
                node = nodes[node_id]
                packet = (aligned_iq_frame(message[index * 16:(index + 1) * 16],
                                           1001, index, frames_count)
                          if node_id == SOURCE else produced[next(edge["source"] for edge in graph["edges"]
                                                                if edge["target"] == node_id)])
                port = next(iter(node["inputs"]))
                started = time.perf_counter()
                output = runtime.process(handles[node_id], {port: packet})
                measurements[node_id]["runtime_ms"].append((time.perf_counter() - started) * 1000)
                measurements[node_id]["input_bytes"].append(packet.nbytes)
                measurements[node_id]["output_bytes"].append(output.nbytes)
                produced[node_id] = output
            outputs.append(produced[SINK])
        elapsed_s = time.perf_counter() - wall_before
        cpu_after = {node_id: cgroup_value(handle.container_id, "cpu.stat", "usage_usec")
                     for node_id, handle in handles.items()}
        memory_peak = {node_id: cgroup_value(handle.container_id, "memory.peak")
                       for node_id, handle in handles.items()}
        if outputs[-1].samples.tobytes() != message:
            raise RuntimeError("标定输入未正确完成数据链重组")
    finally:
        for handle in reversed(list(handles.values())):
            runtime.stop(handle)

    hardware_id = "docker-" + info["ID"].replace(":", "-")
    profiles = []
    raw_nodes = {}
    for node_id in graph["topological_order"]:
        node = nodes[node_id]
        measured = measurements[node_id]
        cpu_usec = cpu_after[node_id] - cpu_before[node_id]
        observed_millicores = cpu_usec / (elapsed_s * 1000)
        # CPU 预留保持与标定时相同的 1 核上限；内存预留给峰值留 50% 余量。
        memory_reservation = math.ceil(max(memory_peak[node_id] * 1.5,
                                           64 * 1024 * 1024) / (1024 * 1024)) * 1024 * 1024
        if memory_reservation > 256 * 1024 * 1024:
            raise RuntimeError(f"{node_id} 的内存峰值超出标定上限，需提高上限重测")
        profiles.append({
            "cid": node["cid"], "implementation_id": f"{node['cid']}-docker-{image_id[7:19]}",
            "processor": "cpu", "hardware_id": hardware_id, "required_features": [],
            "parameters": node["parameters"],
            "demand": asdict(PhysicalUsage(1000, memory_reservation, 0, 0, 0)),
            "runtime_p95_ms": p95(measured["runtime_ms"]),
            "setup_p95_ms": start_ms[node_id],
            "input_bytes": max(measured["input_bytes"]),
            "output_bytes": max(measured["output_bytes"]),
            "sample_count": frames_count,
            "source": "local Docker calibration; 1 CPU/256 MiB limit; startup n=1; CPU reservation=1 core; memory=1.5x peak, min 64 MiB; same-host network=0",
        })
        raw_nodes[node_id] = {
            "cid": node["cid"], "startup_ms": start_ms[node_id],
            "runtime_ms": measured["runtime_ms"],
            "input_bytes": measured["input_bytes"], "output_bytes": measured["output_bytes"],
            "cpu_usage_usec": cpu_usec, "observed_cpu_millicores": observed_millicores,
            "memory_peak_bytes": memory_peak[node_id],
            "memory_reservation_bytes": memory_reservation,
        }
    transfers = [{"source": edge["source"], "target": edge["target"],
                  "bytes_per_call": max(measurements[edge["source"]]["output_bytes"]),
                  "reserved_bytes_per_s": 1}
                 for edge in graph["edges"]]
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "hardware.local.json", {
        "schema_version": 1, "hardware_id": hardware_id,
        "capacity": asdict(PhysicalUsage(info["NCPU"] * 1000, info["MemTotal"], 0, 0, 0)),
        "features": []})
    write_json(output_dir / "measurements.local.json", {"schema_version": 1, "profiles": profiles})
    write_json(output_dir / "config.local.json", {
        "schema_version": 1, "processing_node_id": "local-docker",
        "hardware_profile": "hardware.local.json", "measurements": "measurements.local.json",
        "transfers": transfers, "images": {node["cid"]: IMAGE for node in nodes.values()}})
    write_json(output_dir / "calibration.local.json", {
        "docker_engine_id": info["ID"], "docker_server_version": info["ServerVersion"],
        "image_id": image_id, "frame_count": frames_count,
        "elapsed_processing_s": elapsed_s, "nodes": raw_nodes,
        "notes": ["业务输入是演示用对齐 IQ 帧，不代表真实射频链路。",
                  "启动时间每组件只测一次，字段名沿用 setup_p95_ms，不具有 p95 统计置信度。",
                  "CPU 预留为标定时的 1 核限额；内存预留为观测峰值的 1.5 倍且至少 64 MiB。",
                  "所有组件同机使用回环 HTTP；外部链路带宽未经测量，容量和需求均设为 0。"]})
    return output_dir / "config.local.json"


def main():
    parser = argparse.ArgumentParser(description="标定本机数据链接收容器")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--frames", type=int, default=32)
    args = parser.parse_args()
    print(calibrate(args.output_dir, args.frames))


if __name__ == "__main__":
    main()
