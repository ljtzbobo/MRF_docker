"""演示数据链帧校验与跨帧报文重组。"""

import numpy as np

from ...runtime import DataPacket, RuntimePort
from ...runtime.blocks import BlockComponent


def _crc16(data: bytes) -> int:
    value = 0xffff
    for byte in data:
        value ^= byte << 8
        for _ in range(8):
            value = ((value << 1) ^ 0x1021) & 0xffff if value & 0x8000 else (value << 1) & 0xffff
    return value


def encode_demo_frame(payload: bytes, message_id: int, fragment_index: int,
                      fragment_count: int) -> np.ndarray:
    """生成测试和仿真可用的 demo_crc_frame_v1 比特帧。"""
    if (not isinstance(payload, bytes) or not 1 <= len(payload) <= 16 or
            type(message_id) is not int or not 0 <= message_id < 2**32 or
            type(fragment_count) is not int or not 1 <= fragment_count <= 255 or
            type(fragment_index) is not int or not 0 <= fragment_index < fragment_count):
        raise ValueError("invalid demo data link fragment")
    header = (message_id.to_bytes(4, "big") +
              bytes((fragment_index, fragment_count, len(payload), 0)))
    body = header + payload.ljust(16, b"\0")
    return np.unpackbits(np.frombuffer(body + _crc16(body).to_bytes(2, "big"), dtype=np.uint8))


class DataLinkFrameVerifyComponent(BlockComponent):
    cid = "C35"
    metadata_key = "data_link_fragment"

    def __init__(self, frame_format="demo_crc_frame_v1", crc="crc16",
                 payload_bits=128, header_bits=64, crc_bits=16):
        super().__init__(frame_format=frame_format, crc=crc, payload_bits=payload_bits,
                         header_bits=header_bits, crc_bits=crc_bits)

    def _validate_config(self, frame_format, crc, payload_bits, header_bits, crc_bits):
        if (frame_format != "demo_crc_frame_v1" or crc != "crc16" or
                (payload_bits, header_bits, crc_bits) != (128, 64, 16)):
            raise ValueError("C35 requires the 64+128+16 bit demo CRC16 frame")
        return dict(frame_format=frame_format, crc=crc, payload_bits=payload_bits,
                    header_bits=header_bits, crc_bits=crc_bits)

    def _setup(self):
        self.input_port = RuntimePort("in", "input", ("bits",), ("u",), (1,), "bits")
        self.output_port = RuntimePort("out", "output", ("fragment",), ("u",), (1,), "bytes")

    def _frame_size(self):
        return 208

    def _execute(self, samples):
        if np.any((samples != 0) & (samples != 1)):
            raise ValueError("frame contains non-binary values")
        frame = np.packbits(samples).tobytes()
        if _crc16(frame[:-2]) != int.from_bytes(frame[-2:], "big"):
            raise ValueError("frame CRC16 check failed")
        message_id = int.from_bytes(frame[:4], "big")
        index, count, length, reserved = frame[4:8]
        if not 1 <= count <= 255 or index >= count or not 1 <= length <= 16 or reserved:
            raise ValueError("invalid data link frame header")
        if any(frame[8 + length:24]):
            raise ValueError("nonzero frame payload padding")
        self._business = dict(message_id=message_id, fragment_index=index,
                              fragment_count=count, payload_length=length, crc_ok=True)
        return np.frombuffer(frame[8:8 + length], dtype=np.uint8).copy()

    def _output_context(self, packet):
        return packet.sample_rate / 8, packet.center_frequency, packet.timestamp


class DataLinkReassemblyComponent(BlockComponent):
    cid = "C36"

    def __init__(self, fragment_payload_bytes=16):
        self._pending = {}
        super().__init__(fragment_payload_bytes=fragment_payload_bytes)

    def _validate_config(self, fragment_payload_bytes=16):
        if type(fragment_payload_bytes) is not int or fragment_payload_bytes != 16:
            raise ValueError("C36 requires 16-byte demo fragments")
        return dict(fragment_payload_bytes=fragment_payload_bytes)

    def configure(self, **parameters):
        super().configure(**parameters)
        self._pending.clear()

    def _setup(self):
        self.input_port = RuntimePort("in", "input", ("fragment",), ("u",), (1,), "bytes")
        self.output_port = RuntimePort("out", "output", ("message",), ("u",), (1,), "bytes")

    def _execute(self, samples):
        raise NotImplementedError("use process with verified fragment metadata")

    def push(self, packet):
        raise NotImplementedError("use process in a persistent DAG execution session")

    def reset_buffer(self):
        released = self._buffer_nbytes()
        self._pending.clear()
        super().reset_buffer()
        return released

    def _buffer_nbytes(self):
        return sum(len(payload) for count, parts in self._pending.values()
                   for payload in parts.values())

    def process(self, packet):
        def action():
            self.input_port.validate(packet)
            if packet.samples.dtype != np.uint8 or not 1 <= packet.item_count <= 16:
                raise ValueError("fragment must contain 1..16 uint8 bytes")
            info = packet.metadata.get("data_link_fragment")
            if not isinstance(info, dict) or info.get("crc_ok") is not True:
                raise ValueError("fragment requires verified CRC metadata")
            message_id = info.get("message_id")
            index = info.get("fragment_index")
            count = info.get("fragment_count")
            if (type(message_id) is not int or not 0 <= message_id < 2**32 or
                    type(count) is not int or not 1 <= count <= 255 or
                    type(index) is not int or not 0 <= index < count or
                    info.get("payload_length") != packet.item_count or
                    index != count - 1 and packet.item_count != 16):
                raise ValueError("invalid verified fragment metadata")
            payload = packet.samples.tobytes()
            old_count, old_parts = self._pending.get(message_id, (count, {}))
            if old_count != count or index in old_parts and old_parts[index] != payload:
                raise ValueError("conflicting message fragment")
            parts = dict(old_parts)
            parts[index] = payload
            if len(parts) == count:
                data = b"".join(parts[i] for i in range(count))
                self._pending.pop(message_id, None)
                status = "delivered"
            else:
                self._pending[message_id] = (count, parts)
                data = b""
                status = "pending"
            self._observe_buffer(self._buffer_nbytes())
            metadata = dict(packet.metadata)
            metadata["data_link_message"] = dict(message_id=message_id, status=status,
                                                   received_fragments=len(parts),
                                                   fragment_count=count, length_bytes=len(data))
            return DataPacket(np.frombuffer(data, dtype=np.uint8), packet.sample_rate,
                              packet.center_frequency, packet.timestamp, "message", metadata)
        return self._measure_call("process", packet, action)
