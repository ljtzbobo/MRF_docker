"""双输入复数 IQ 合成，用于分支汇聚执行图。"""

import math

from ...runtime import DataPacket, RuntimePort
from ...runtime.blocks import BlockComponent


class IQMergeComponent(BlockComponent):
    cid = "C37"

    def __init__(self, gain_left=1.0, gain_right=1.0):
        super().__init__(gain_left=gain_left, gain_right=gain_right)

    def _validate_config(self, gain_left=1.0, gain_right=1.0):
        if any(type(value) not in (int, float) or not math.isfinite(value)
               for value in (gain_left, gain_right)):
            raise ValueError("IQ merge gains must be finite numbers")
        return dict(gain_left=gain_left, gain_right=gain_right)

    def _setup(self):
        self.input_ports = {
            name: RuntimePort(name, "input", ("time",), ("c",), (1,), "samples")
            for name in ("left", "right")
        }
        self.input_port = self.input_ports["left"]
        self.output_port = RuntimePort("out", "output", ("time",), ("c",), (1,), "samples")

    def _execute(self, samples):
        raise NotImplementedError("use process_inputs for both IQ branches")

    def process(self, packet):
        raise ValueError("IQ merge requires left and right input packets")

    def push(self, packet):
        raise NotImplementedError("IQ merge requires process_inputs with both ports")

    def process_inputs(self, packets):
        def action():
            if set(packets) != {"left", "right"}:
                raise ValueError("IQ merge requires left and right inputs")
            left, right = packets["left"], packets["right"]
            self.input_ports["left"].validate(left)
            self.input_ports["right"].validate(right)
            if (left.samples.shape != right.samples.shape or
                    left.sample_rate != right.sample_rate or
                    left.center_frequency != right.center_frequency or
                    left.timestamp != right.timestamp):
                raise ValueError("IQ merge inputs have incompatible shape or signal context")
            data = self._parameters["gain_left"] * left.samples + self._parameters["gain_right"] * right.samples
            return DataPacket(data, left.sample_rate, left.center_frequency,
                              left.timestamp, "time", left.metadata)
        return self._measure_call("process_inputs", packets, action)
