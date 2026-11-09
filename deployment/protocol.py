"""容器端口的 JSON 数据包协议，数组载荷使用 base64 保留精确 dtype。"""

import base64
import binascii
from math import prod

import numpy as np

from components.runtime import DataPacket


MAX_PACKET_BYTES = 16 * 1024 * 1024


def encode_packet(packet: DataPacket) -> dict:
    if not isinstance(packet, DataPacket) or packet.nbytes > MAX_PACKET_BYTES:
        raise ValueError("packet must be a DataPacket no larger than 16 MiB")
    samples = np.ascontiguousarray(packet.samples)
    return {"dtype": samples.dtype.str, "shape": list(samples.shape),
            "data_base64": base64.b64encode(samples.tobytes()).decode("ascii"),
            "sample_rate": packet.sample_rate,
            "center_frequency": packet.center_frequency,
            "timestamp": packet.timestamp, "domain": packet.domain,
            "metadata": dict(packet.metadata)}


def decode_packet(raw: dict) -> DataPacket:
    if not isinstance(raw, dict) or set(raw) != {
            "dtype", "shape", "data_base64", "sample_rate", "center_frequency",
            "timestamp", "domain", "metadata"}:
        raise ValueError("packet protocol fields are incomplete")
    try:
        dtype = np.dtype(raw["dtype"])
        shape = raw["shape"]
        if (dtype.kind not in "biufc" or dtype.hasobject or
                not isinstance(shape, list) or len(shape) not in (1, 2) or
                any(type(size) is not int or size < 0 for size in shape) or
                len(shape) == 2 and 0 in shape or
                prod(shape) * dtype.itemsize > MAX_PACKET_BYTES):
            raise ValueError("packet dtype or shape is invalid")
        payload = base64.b64decode(raw["data_base64"], validate=True)
        if len(payload) != prod(shape) * dtype.itemsize:
            raise ValueError("packet payload length does not match shape")
        samples = np.frombuffer(payload, dtype=dtype).reshape(shape)
        return DataPacket(samples, raw["sample_rate"], raw["center_frequency"],
                          raw["timestamp"], raw["domain"], raw["metadata"])
    except (TypeError, KeyError, binascii.Error) as exc:
        raise ValueError(f"invalid packet protocol: {exc}") from exc
