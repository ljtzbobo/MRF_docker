"""Datasheet capacity ceilings; region floorplan and kernel demands remain modeled."""
from .measurements import FPGA_FIELDS

REFERENCE_MODEL = "66AK2H06-XC7K410T-v1"
DSP_CLOCK_MCYCLES_PER_S = 1200
DSP_LOCAL_L2_BYTES = 1024 * 1024
FPGA_TOTAL = dict(fpga_lut=254200, fpga_ff=508400, fpga_dsp48=1540, fpga_bram36=795)
# Model policy: 20% static shell reserve, 80% divided into four equal logical regions.
FPGA_REGION = dict(fpga_lut=50840, fpga_ff=101680, fpga_dsp48=308, fpga_bram36=159)


def validate_reference_nodes(nodes):
    groups = {processor: [n for n in nodes if n.processor == processor]
              for processor in ("dsp", "fpga")}
    for processor, units in groups.items():
        if (len(units) != 4 or {n.unit_index for n in units} != set(range(4)) or
                len({n.device_id for n in units}) != 1 or not units[0].device_id):
            raise ValueError(f"reference {processor} requires one device with exactly four distinct indexed units")
    for node in groups['dsp']:
        capacity = node.hardware.capacity
        if (not 0 < capacity.dsp_compute_mcycles_per_s <= DSP_CLOCK_MCYCLES_PER_S or
                not 0 < capacity.dsp_local_memory_bytes <= DSP_LOCAL_L2_BYTES or
                capacity.dsp_units or capacity.fpga_units or
                any(getattr(capacity, key) for key in FPGA_FIELDS)):
            raise ValueError("C66x core exceeds reference compute/local L2 budget or uses abstract slots")
    for node in groups['fpga']:
        capacity = node.hardware.capacity
        if (capacity.fpga_units or capacity.dsp_units or capacity.dsp_compute_mcycles_per_s or
                capacity.dsp_local_memory_bytes or
                any(not 0 < getattr(capacity, key) <= FPGA_REGION[key] for key in FPGA_FIELDS)):
            raise ValueError("FPGA region exceeds four-dimensional reference budget or uses abstract slots")
    if {n.device_id for n in groups['dsp']} & {n.device_id for n in groups['fpga']}:
        raise ValueError("DSP and FPGA must have distinct device identities")
    if len({n.hardware.hardware_id for n in nodes}) != len(nodes):
        raise ValueError("every reference core/region requires a unique hardware_id")
