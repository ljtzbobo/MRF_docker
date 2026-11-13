"""Explicit synthetic CPU/DSP/FPGA resource configuration; no hardware discovery."""
from dataclasses import dataclass
import json
from pathlib import Path

from .measurements import (PhysicalHardwareProfile, PhysicalUsage, load_measurements,
                           LEGACY_USAGE_FIELDS, _usage)
from .reference_devices import REFERENCE_MODEL, validate_reference_nodes
from .placement import (EdgeTransfer, NetworkLink, PlacementPlanner,
                        PlacementState, ProcessingNode, _fits)


@dataclass(frozen=True)
class ResourceSimulation:
    planner: PlacementPlanner
    initial_state: PlacementState


def load_resource_simulation(path):
    path = Path(path).resolve()
    raw = json.loads(path.read_text(encoding="utf-8"))
    required = {"schema_version", "mode", "nodes", "links", "profiles", "transfers", "initial_state"}
    version = raw.get("schema_version") if isinstance(raw, dict) else None
    if version == 2:
        required.add("reference_model")
    if (not isinstance(raw, dict) or set(raw) != required or
            type(version) is not int or version not in (1, 2) or
            raw["mode"] != "software_simulation"):
        raise ValueError("simulation requires schema_version 1 or 2 and explicit software_simulation mode")
    usage_fields = set(PhysicalUsage.__dataclass_fields__) if version == 2 else LEGACY_USAGE_FIELDS
    if version == 2 and raw['reference_model'] != REFERENCE_MODEL:
        raise ValueError("unknown reference hardware model")
    nodes = []
    for row in raw["nodes"]:
        fields = {"node_id", "hardware_id", "capacity", "features"}
        if version == 2:
            fields |= {"device_id", "processor", "unit_index"}
        if set(row) != fields:
            raise ValueError("invalid simulated node fields")
        if not isinstance(row["node_id"], str) or not row["node_id"].strip():
            raise ValueError("simulated node_id must be nonempty")
        if set(row["capacity"]) != usage_fields:
            raise ValueError("simulated capacity must define every versioned resource dimension")
        if not isinstance(row["features"], list):
            raise ValueError("simulated features must be a list")
        if version == 2 and (row['processor'] not in ('cpu', 'dsp', 'fpga') or
                not isinstance(row['device_id'], str) or not row['device_id'] or
                (row['processor'] != 'cpu' and type(row['unit_index']) is not int)):
            raise ValueError("invalid processor device/unit identity")
        nodes.append(ProcessingNode(row["node_id"], PhysicalHardwareProfile(
            row["hardware_id"], _usage(row["capacity"]), tuple(row["features"])),
            row.get('device_id'), row.get('processor'), row.get('unit_index')))
    if not nodes:
        raise ValueError("simulation needs at least one processing node")
    if version == 2:
        validate_reference_nodes(nodes)
    profiles = load_measurements(path.parent / raw["profiles"])
    if not profiles.profiles or any(p.timing_scope != "synthetic" for p in profiles.profiles):
        raise ValueError("software simulation accepts explicitly synthetic profiles only")
    hardware_ids = {n.hardware.hardware_id for n in nodes}
    if any(p.hardware_id not in hardware_ids for p in profiles.profiles):
        raise ValueError("profile references unknown simulated hardware")
    if version == 2:
        by_hardware = {n.hardware.hardware_id: n for n in nodes}
        for profile in profiles.profiles:
            if (profile.processor != by_hardware[profile.hardware_id].processor or
                    profile.demand.dsp_units or profile.demand.fpga_units):
                raise ValueError("reference profiles must match processor and use explicit core/region dimensions")
    planner = PlacementPlanner(tuple(nodes), tuple(NetworkLink(**r) for r in raw["links"]),
                               profiles, tuple(EdgeTransfer(**r) for r in raw["transfers"]))
    initial = raw["initial_state"]
    if set(initial) != {"node_used", "link_used"} or set(initial["node_used"]) != set(planner.nodes):
        raise ValueError("initial state must cover every simulated node")
    if any(set(value) != usage_fields for value in initial["node_used"].values()):
        raise ValueError("initial node usage must define every versioned resource dimension")
    used = {key: PhysicalUsage(**value) for key, value in initial["node_used"].items()}
    for key, usage in used.items():
        # Reuse the validated hardware resource schema for finite, nonnegative values.
        PhysicalHardwareProfile("state-validation", usage)
        if not _fits(usage, planner.nodes[key].hardware.capacity):
            raise ValueError("initial usage exceeds node capacity")
    links = {}
    for row in initial["link_used"]:
        if set(row) != {"source", "target", "used_bytes_per_s"}:
            raise ValueError("invalid initial link usage")
        pair = (row["source"], row["target"])
        value = row["used_bytes_per_s"]
        PhysicalHardwareProfile("link-state-validation", PhysicalUsage(bandwidth_bytes_per_s=value))
        if pair not in planner.links or pair in links or value > planner.links[pair].capacity_bytes_per_s:
            raise ValueError("invalid or excessive initial link usage")
        links[pair] = value
    if set(links) != set(planner.links):
        raise ValueError("initial state must cover every directed link")
    return ResourceSimulation(planner, PlacementState(used, links))
