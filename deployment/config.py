"""本机 Docker 部署配置；硬件容量和组件测量由调用方显式提供。"""

import json
from pathlib import Path

from resources.measurements import load_measurements, load_physical_hardware
from resources.placement import EdgeTransfer, PlacementPlanner, ProcessingNode
from scheduler import TaskScheduler

from .docker_runtime import DockerRuntime
from .session import DeploymentManager


def load_deployment_manager(path: str | Path) -> DeploymentManager:
    path = Path(path).resolve()
    config = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(config, dict) and config.get("mode") == "software_simulation":
        from .simulation import load_simulation_manager
        return load_simulation_manager(path)
    required = {"schema_version", "processing_node_id", "hardware_profile",
                "measurements", "transfers", "images"}
    if not isinstance(config, dict) or not required <= set(config) or set(config)-required-{'checkpoint_store','pool_state'} or config["schema_version"] != 1:
        raise ValueError("deployment config requires schema_version 1 and deployment fields")
    node_id = config["processing_node_id"]
    if not isinstance(node_id, str) or not node_id:
        raise ValueError("processing_node_id must be a nonempty string")
    hardware = load_physical_hardware(path.parent / config["hardware_profile"])
    measurements = load_measurements(path.parent / config["measurements"])
    transfers = tuple(EdgeTransfer(**row) for row in config["transfers"])
    images = config["images"]
    if not isinstance(images, dict):
        raise ValueError("images must map CID to Docker image")
    planner = PlacementPlanner((ProcessingNode(node_id, hardware),), (), measurements, transfers)
    manager=DeploymentManager(TaskScheduler(placement_planner=planner), DockerRuntime(node_id, images))
    if config.get('checkpoint_store'):manager.pool.configure_checkpoints(path.parent/config['checkpoint_store'])
    if config.get('pool_state'):manager.pool.configure_persistence(path.parent/config['pool_state'])
    return manager
