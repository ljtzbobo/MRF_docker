"""带来源的执行画像；支持实测与显式 synthetic 模型，单位不与归一化等级混用。"""

from dataclasses import MISSING, asdict, dataclass
import json
import math
from pathlib import Path


def execution_profile_basis(profiles):
    scopes = {item.timing_scope for item in profiles}
    if scopes == {"synthetic"}:
        return "synthetic"
    if "synthetic" in scopes:
        return "mixed"
    return "unspecified" if not scopes or "unspecified" in scopes else "measured"


@dataclass(frozen=True)
class PhysicalUsage:
    cpu_millicores: float = 0.0
    memory_bytes: float = 0.0
    bandwidth_bytes_per_s: float = 0.0
    dsp_units: float = 0.0
    fpga_units: float = 0.0
    dsp_compute_mcycles_per_s: float = 0.0
    dsp_local_memory_bytes: float = 0.0
    fpga_lut: float = 0.0
    fpga_ff: float = 0.0
    fpga_dsp48: float = 0.0
    fpga_bram36: float = 0.0


LEGACY_USAGE_FIELDS = {"cpu_millicores", "memory_bytes", "bandwidth_bytes_per_s", "dsp_units", "fpga_units"}
FPGA_FIELDS = ("fpga_lut", "fpga_ff", "fpga_dsp48", "fpga_bram36")
DSP_FIELDS = ("dsp_units", "dsp_compute_mcycles_per_s", "dsp_local_memory_bytes")


def validate_processor_demand(processor, demand):
    dsp = any(getattr(demand, key) > 0 for key in DSP_FIELDS)
    fpga = demand.fpga_units > 0 or any(getattr(demand, key) > 0 for key in FPGA_FIELDS)
    primary = {"cpu": demand.cpu_millicores > 0,
               "dsp": demand.dsp_units > 0 or demand.dsp_compute_mcycles_per_s > 0,
               "fpga": fpga}[processor]
    if not primary or (processor != "dsp" and dsp) or (processor != "fpga" and fpga):
        raise ValueError("measurement has inconsistent processor demand")


def _validate_usage(usage: PhysicalUsage) -> None:
    for name, value in asdict(usage).items():
        _number(value, name)
        if name in FPGA_FIELDS and value != int(value):
            raise ValueError(f"{name} must count whole hardware resources")


@dataclass(frozen=True)
class PhysicalHardwareProfile:
    hardware_id: str
    capacity: PhysicalUsage
    features: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (not isinstance(self.hardware_id, str) or not self.hardware_id.strip() or
                not isinstance(self.features, tuple) or
                any(not isinstance(item, str) or not item for item in self.features) or
                len(set(self.features)) != len(self.features)):
            raise ValueError("hardware_id and features must be valid")
        _validate_usage(self.capacity)


@dataclass(frozen=True)
class PhysicalResourceState:
    hardware_id: str
    used: PhysicalUsage

    def __post_init__(self) -> None:
        if not isinstance(self.hardware_id, str) or not self.hardware_id.strip():
            raise ValueError("state hardware_id must be nonempty")
        _validate_usage(self.used)


@dataclass(frozen=True)
class ComponentMeasurement:
    cid: str
    implementation_id: str
    processor: str
    hardware_id: str
    required_features: tuple[str, ...]
    parameters: dict
    demand: PhysicalUsage
    runtime_p95_ms: float
    setup_p95_ms: float
    input_bytes: int
    output_bytes: int
    sample_count: int
    source: str
    timing_scope: str = "unspecified"
    setup_sample_count: int = 1
    tuning_setup_ms: float = 0.0

    @property
    def effective_setup_ms(self):
        return self.setup_p95_ms + self.tuning_setup_ms

    def __post_init__(self) -> None:
        _number(self.tuning_setup_ms, 'tuning_setup_ms')
        if self.timing_scope not in ("unspecified", "component_process", "docker_http_roundtrip", "synthetic"):
            raise ValueError("unknown measurement timing_scope")
        if type(self.setup_sample_count) is not int or self.setup_sample_count < 1:
            raise ValueError("setup_sample_count must be positive")
        if any(not isinstance(value, str) or not value.strip() for value in
               (self.cid, self.implementation_id, self.hardware_id, self.source)):
            raise ValueError("measurement identifiers and source must be nonempty")
        if self.processor not in ("cpu", "dsp", "fpga"):
            raise ValueError("measurement processor must be cpu, dsp or fpga")
        if (not isinstance(self.parameters, dict) or
                not isinstance(self.required_features, tuple) or
                any(not isinstance(value, str) or not value for value in self.required_features) or
                len(set(self.required_features)) != len(self.required_features)):
            raise ValueError("measurement parameters and required_features must be valid")
        _validate_usage(self.demand)
        validate_processor_demand(self.processor, self.demand)
        _number(self.runtime_p95_ms, "runtime_p95_ms", positive=True)
        _number(self.setup_p95_ms, "setup_p95_ms")
        for name in ("input_bytes", "output_bytes", "sample_count"):
            value = getattr(self, name)
            if type(value) is not int or value < (1 if name == "sample_count" else 0):
                raise ValueError(f"{name} must be a nonnegative integer")

    def applies_to(self, cid: str, parameters: dict,
                   hardware: PhysicalHardwareProfile) -> bool:
        return (self.cid == cid and self.parameters == parameters and
                self.hardware_id == hardware.hardware_id and
                set(self.required_features) <= set(hardware.features))


@dataclass(frozen=True)
class MeasurementCatalog:
    profiles: tuple[ComponentMeasurement, ...] = ()

    def candidates(self, cid: str, parameters: dict,
                   hardware: PhysicalHardwareProfile) -> tuple[ComponentMeasurement, ...]:
        return tuple(item for item in self.profiles if item.applies_to(cid, parameters, hardware))


def _number(value, name: str, *, positive: bool = False) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or positive and value == 0:
        raise ValueError(f"{name} must be a finite {'positive' if positive else 'nonnegative'} number")
    return float(value)


def _usage(raw: dict) -> PhysicalUsage:
    keys = set(PhysicalUsage.__dataclass_fields__)
    if not isinstance(raw, dict) or set(raw) not in (keys, LEGACY_USAGE_FIELDS):
        raise ValueError(f"demand must contain the legacy five or all {len(keys)} resource fields")
    result = PhysicalUsage(**{key: _number(raw[key], key) for key in raw})
    _validate_usage(result)
    return result


def load_measurements(path: str | Path) -> MeasurementCatalog:
    """只接收有来源、样本数和精确参数的标定记录。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "profiles"} or type(raw["schema_version"]) is not int or raw["schema_version"] not in (1, 2) or not isinstance(raw["profiles"], list):
        raise ValueError("measurement catalog requires schema_version 1 or 2 and profiles array")
    fields = ComponentMeasurement.__dataclass_fields__
    required = {key for key, field in fields.items() if field.default is MISSING}
    profiles = []
    for index, row in enumerate(raw["profiles"]):
        if not isinstance(row, dict) or not required <= set(row) or set(row) - set(fields):
            raise ValueError(f"profiles[{index}] has missing or unknown fields")
        for key in ("cid", "implementation_id", "hardware_id", "source"):
            if not isinstance(row[key], str) or not row[key].strip():
                raise ValueError(f"profiles[{index}].{key} must be nonempty")
        if type(row["processor"]) is not str or row["processor"] not in ("cpu", "dsp", "fpga"):
            raise ValueError(f"profiles[{index}].processor must be cpu, dsp or fpga")
        if (not isinstance(row["required_features"], list) or
                any(not isinstance(item, str) or not item for item in row["required_features"]) or
                len(set(row["required_features"])) != len(row["required_features"])):
            raise ValueError(f"profiles[{index}].required_features must be strings")
        if not isinstance(row["parameters"], dict):
            raise ValueError(f"profiles[{index}].parameters must be an object")
        demand = _usage(row["demand"])
        if raw["schema_version"] == 2 and set(row["demand"]) != set(PhysicalUsage.__dataclass_fields__):
            raise ValueError("schema 2 requires all resource dimensions")
        validate_processor_demand(row["processor"], demand)
        times = {key: _number(row[key], key, positive=key == "runtime_p95_ms")
                 for key in ("runtime_p95_ms", "setup_p95_ms")}
        for key in ("input_bytes", "output_bytes", "sample_count"):
            if type(row[key]) is not int or row[key] < (1 if key == "sample_count" else 0):
                raise ValueError(f"profiles[{index}].{key} must be a nonnegative integer")
        profiles.append(ComponentMeasurement(
            cid=row["cid"], implementation_id=row["implementation_id"],
            processor=row["processor"], hardware_id=row["hardware_id"],
            required_features=tuple(row["required_features"]), parameters=row["parameters"],
            demand=demand, input_bytes=row["input_bytes"], output_bytes=row["output_bytes"],
            sample_count=row["sample_count"], source=row["source"],
            timing_scope=row.get("timing_scope", "unspecified"),
            setup_sample_count=row.get("setup_sample_count", 1),
            tuning_setup_ms=row.get("tuning_setup_ms", 0), **times))
    return MeasurementCatalog(tuple(profiles))


def save_measurements(path: str | Path, catalog: MeasurementCatalog) -> None:
    """Write version 1 catalogs, including optional measurement scope metadata."""
    extended = any(getattr(p.demand, key) for p in catalog.profiles
                   for key in set(PhysicalUsage.__dataclass_fields__) - LEGACY_USAGE_FIELDS)
    Path(path).write_text(json.dumps({"schema_version": 2 if extended else 1,
                                    "profiles": [asdict(item) for item in catalog.profiles]},
                                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_physical_hardware(path: str | Path) -> PhysicalHardwareProfile:
    """读取带明确单位的设备容量，不从 0–100 等级换算。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "hardware_id", "capacity", "features"} or type(raw["schema_version"]) is not int or raw["schema_version"] not in (1, 2):
        raise ValueError("physical hardware requires schema_version 1, hardware_id, capacity and features")
    if not isinstance(raw["hardware_id"], str) or not raw["hardware_id"].strip():
        raise ValueError("hardware_id must be nonempty")
    features = raw["features"]
    if (not isinstance(features, list) or any(not isinstance(item, str) or not item for item in features)
            or len(set(features)) != len(features)):
        raise ValueError("features must be unique nonempty strings")
    if raw["schema_version"] == 2 and set(raw["capacity"]) != set(PhysicalUsage.__dataclass_fields__):
        raise ValueError("schema 2 requires all resource dimensions")
    return PhysicalHardwareProfile(raw["hardware_id"], _usage(raw["capacity"]), tuple(features))
