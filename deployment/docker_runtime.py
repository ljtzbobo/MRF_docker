"""本机 Docker CLI 容器生命周期和回环 HTTP 端口传输。"""

from dataclasses import dataclass
import json
import subprocess
from threading import Lock
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from .protocol import decode_packet, encode_packet


class DockerRuntimeError(RuntimeError):
    pass


class DockerCleanupError(DockerRuntimeError):
    pass


@dataclass(frozen=True)
class ContainerHandle:
    container_id: str
    endpoint: str
    unit_id: str


class DockerRuntime:
    """一个逻辑处理节点映射到当前 Docker Engine；远程节点须另配适配器。"""

    def __init__(self, processing_node_id: str, images: dict[str, str],
                 *, docker_bin: str = "docker", ready_timeout_s: float = 15):
        if not processing_node_id or not images or any(not cid or not image for cid, image in images.items()):
            raise ValueError("Docker runtime requires processing node and CID images")
        self.processing_node_id = processing_node_id
        self.images = dict(images)
        self.docker_bin = docker_bin
        self.ready_timeout_s = ready_timeout_s
        self._handles: dict[str, ContainerHandle] = {}
        self._handles_lock = Lock()

    def _docker(self, *args):
        try:
            result = subprocess.run([self.docker_bin, *args], capture_output=True,
                                    text=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DockerRuntimeError(f"Docker CLI unavailable: {exc}") from exc
        if result.returncode:
            raise DockerRuntimeError(result.stderr.strip() or result.stdout.strip() or
                                     f"docker {args[0]} failed")
        return result.stdout.strip()

    def preflight(self, allocations):
        self._docker("info", "--format", "{{.ServerVersion}}")
        images = set()
        for item in allocations:
            if item.processing_node_id != self.processing_node_id:
                raise DockerRuntimeError(f"{item.processing_node_id} is not this Docker host")
            if item.processor != "cpu":
                raise DockerRuntimeError(f"{item.cid} requires {item.processor}; Docker runtime supports CPU only")
            image = self.images.get(item.cid)
            if image is None:
                raise DockerRuntimeError(f"no Docker image for {item.cid}")
            images.add(image)
        for image in images:
            self._docker("image", "inspect", image, "--format", "{{.Id}}")

    def start(self, allocation, node: dict) -> ContainerHandle:
        if (allocation.processing_node_id != self.processing_node_id or
                node["cid"] != allocation.cid or allocation.processor != "cpu"):
            raise DockerRuntimeError("allocation and component graph disagree")
        image = self.images[allocation.cid]
        name = f"mrf-{uuid4().hex[:20]}"
        configuration = {"id": node["id"], "cid": node["cid"],
                         "parameters": node["parameters"], "inputs": node["inputs"],
                         "outputs": node["outputs"]}
        args = ["run", "--detach", "--name", name,
                "--label", f"mrf.task_id={allocation.container_unit_id.split(':', 1)[0]}",
                "--publish", "127.0.0.1::8765"]
        if allocation.demand.cpu_millicores > 0:
            args.extend(["--cpus", str(allocation.demand.cpu_millicores / 1000)])
        if allocation.demand.memory_bytes > 0:
            args.extend(["--memory", str(int(allocation.demand.memory_bytes))])
        args.extend([image, "python", "-m", "deployment.worker", "--cid", allocation.cid,
                     "--node-json", json.dumps(configuration, separators=(",", ":"))])
        container_id = self._docker(*args).splitlines()[-1]
        handle = ContainerHandle(container_id, "", allocation.container_unit_id)
        try:
            port = self._docker("port", container_id, "8765/tcp").splitlines()[0].rsplit(":", 1)[-1]
            endpoint = f"http://127.0.0.1:{int(port)}"
            handle = ContainerHandle(container_id, endpoint, allocation.container_unit_id)
            deadline = time.monotonic() + self.ready_timeout_s
            while time.monotonic() < deadline:
                try:
                    with urlopen(endpoint + "/health", timeout=0.5) as response:
                        if json.load(response).get("cid") == allocation.cid:
                            with self._handles_lock:
                                self._handles[handle.unit_id] = handle
                            return handle
                except (OSError, ValueError, URLError):
                    time.sleep(0.1)
            try:
                logs = self._docker("logs", "--tail", "30", container_id)
            except DockerRuntimeError as exc:
                logs = str(exc)
            raise DockerRuntimeError(
                f"container {allocation.container_unit_id} did not become ready: {logs}")
        except Exception as start_error:
            try:
                self.stop(handle)
            except DockerRuntimeError as cleanup_error:
                raise DockerCleanupError(
                    f"container {handle.container_id} failed to start and could not be removed: "
                    f"{cleanup_error}") from start_error
            raise

    def process(self, handle: ContainerHandle, packets: dict):
        body = json.dumps({"inputs": {port: encode_packet(packet)
                                      for port, packet in packets.items()}},
                          separators=(",", ":")).encode("utf-8")
        request = Request(handle.endpoint + "/process", body,
                          {"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=30) as response:
                return decode_packet(json.load(response)["output"])
        except HTTPError as exc:
            try:
                detail = json.load(exc).get("error", str(exc))
            except (ValueError, OSError):
                detail = str(exc)
            raise DockerRuntimeError(f"{handle.unit_id}: {detail}") from exc
        except (OSError, ValueError, KeyError, URLError) as exc:
            raise DockerRuntimeError(f"{handle.unit_id}: packet transport failed: {exc}") from exc

    def stop(self, handle: ContainerHandle):
        try:
            self._docker("rm", "--force", handle.container_id)
        except DockerRuntimeError as exc:
            if "No such container" not in str(exc):
                raise
        with self._handles_lock:
            self._handles.pop(handle.unit_id, None)

    def snapshot(self) -> dict[str, dict]:
        """按运行单元读取 Docker 实时统计，单个容器失败不影响资源快照。"""
        with self._handles_lock:
            handles = tuple(self._handles.values())
        result = {}
        for handle in handles:
            try:
                raw = self._docker("stats", "--no-stream", "--format", "{{json .}}",
                                   handle.container_id)
                stats = json.loads(raw.splitlines()[0])
                result[handle.unit_id] = {
                    "container_id": handle.container_id,
                    "status": "RUNNING",
                    "cpu_percent": stats["CPUPerc"],
                    "memory_usage": stats["MemUsage"],
                    "network_io": stats["NetIO"],
                }
            except (DockerRuntimeError, ValueError, KeyError, IndexError) as exc:
                result[handle.unit_id] = {
                    "container_id": handle.container_id,
                    "status": "UNAVAILABLE", "error": str(exc),
                }
        return result
