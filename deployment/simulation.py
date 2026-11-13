"""Software-only execution of virtual DSP/FPGA allocations using CPU reference kernels.

Resource quantities and times are model inputs. No HDL, instruction-set emulation,
Docker containers or hardware acceleration is claimed.
"""
from threading import RLock
from contextlib import ExitStack

from components.implementations.runtime_registry import create_runtime_component
from resources.simulation import load_resource_simulation
from scheduler import TaskScheduler

from .protocol import decode_packet, encode_packet
from .session import DeploymentManager


class SoftwareSimulationRuntime:
    execution_mode = "software_simulation"

    def __init__(self, planner):
        self.planner = planner
        self.instances = {}
        self._unit_locks = {}
        self.lock = RLock()

    def _profile(self, allocation, parameters=None):
        host = self.planner.nodes.get(allocation.processing_node_id)
        if host is None:
            raise ValueError("unknown simulated processing node")
        for profile in self.planner.measurements.profiles:
            if (profile.hardware_id == host.hardware.hardware_id and
                    profile.cid == allocation.cid and
                    profile.implementation_id == allocation.implementation_id and
                    profile.processor == allocation.processor and
                    profile.demand == allocation.demand and
                    profile.runtime_p95_ms == allocation.runtime_p95_ms and
                    profile.effective_setup_ms == allocation.setup_p95_ms and
                    (parameters is None or profile.parameters == parameters) and
                    profile.timing_scope == "synthetic" and
                    set(profile.required_features) <= set(host.hardware.features)):
                return profile
        raise ValueError("allocation has no compatible synthetic execution profile")

    def preflight(self, allocations):
        for allocation in allocations:
            profile = self._profile(allocation)
            # Fail before reserving or starting instances if the kernel is unavailable.
            create_runtime_component(profile.cid, **profile.parameters)

    def start(self, allocation, node):
        profile = self._profile(allocation, node["parameters"])
        if node["cid"] != profile.cid or node["parameters"] != profile.parameters:
            raise ValueError("simulation allocation and execution graph disagree")
        component = create_runtime_component(node["cid"], **node["parameters"])
        with self.lock:
            if allocation.container_unit_id in self.instances:
                raise ValueError("simulated runtime unit already exists")
            self.instances[allocation.container_unit_id] = (allocation, component, profile)
            self._unit_locks[allocation.container_unit_id] = RLock()
        return allocation.container_unit_id

    def process(self, handle, packets):
        with self.lock:
            unit_lock = self._unit_locks[handle]
        # Different reference instances may run independently. Serialize only this instance.
        with unit_lock:
            with self.lock:
                allocation, component, profile = self.instances[handle]
            if sum(packet.samples.nbytes for packet in packets.values()) > profile.input_bytes:
                raise ValueError("input exceeds synthetic profile byte envelope")
            inputs = {port: decode_packet(encode_packet(packet)) for port, packet in packets.items()}
            output = (component.process(next(iter(inputs.values()))) if len(inputs) == 1
                      else component.process_inputs(inputs))
            if output.samples.nbytes > profile.output_bytes:
                raise ValueError("output exceeds synthetic profile byte envelope")
            return decode_packet(encode_packet(output))

    def stop(self, handle):
        with self.lock:
            unit_lock = self._unit_locks.get(handle)
        if unit_lock is not None:
            with unit_lock, self.lock:
                self.instances.pop(handle, None)
                self._unit_locks.pop(handle, None)

    def pool_identity(self, cid):
        return "python_cpu_reference:v1:" + cid

    def health(self, handle):
        with self.lock:
            if handle not in self.instances:
                raise ValueError("simulated runtime unit is missing")

    def reset(self, handle, allocation, node):
        profile = self._profile(allocation, node["parameters"])
        component = create_runtime_component(node["cid"], **node["parameters"])
        with self.lock:
            if handle not in self.instances:
                raise ValueError("cannot reset a missing runtime unit")
            unit_lock = self._unit_locks[handle]
        with unit_lock, self.lock:
            if handle not in self.instances:
                raise ValueError("cannot reset a missing runtime unit")
            self.instances[handle] = (allocation, component, profile)

    def snapshot(self):
        with self.lock:
            return {key: {"status": "SIMULATED", "execution_mode": self.execution_mode,
                          "execution_backend": "python_cpu_reference",
                          "modeled_processor": allocation.processor}
                    for key, (allocation, _, _) in self.instances.items()}

    def rebind_many(self, changes):
        with self.lock:
            locks = [self._unit_locks[handle] for handle,_,_ in sorted(changes,key=lambda row:row[0])]
        with ExitStack() as stack:
            for lock in locks:stack.enter_context(lock)
            with self.lock:
                replacements = {}
                for handle,allocation,node in changes:
                    old,component,_ = self.instances[handle]
                    profile = self._profile(allocation,node['parameters'])
                    if old.cid != allocation.cid:raise ValueError('cannot change component function during rebinding')
                    replacements[handle] = (allocation,component,profile)
                undo = {handle:self.instances[handle] for handle in replacements}
                self.instances.update(replacements)
                return undo

    def restore_bindings(self, previous):
        with self.lock:self.instances.update(previous)


def load_simulation_manager(path):
    environment = load_resource_simulation(path)
    scheduler = TaskScheduler(placement_planner=environment.planner,
                              placement_baseline=environment.initial_state)
    return DeploymentManager(scheduler, SoftwareSimulationRuntime(environment.planner))
