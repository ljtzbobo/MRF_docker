"""Single RF front-end tuning constraints and virtual LO settling cost."""
from dataclasses import dataclass, replace
from threading import RLock
from .._signal import real


@dataclass(frozen=True)
class RFOperatingRange:
    min_frequency_hz: float = 30e6
    max_frequency_hz: float = 6e9
    min_bandwidth_hz: float = 1000
    max_bandwidth_hz: float = 40e6
    max_sample_rate_hz: float = 61.44e6

    def __post_init__(self):
        for key,value in vars(self).items():
            if real(value,key) <= 0: raise ValueError('RF limits must be positive')
        if self.min_frequency_hz >= self.max_frequency_hz or self.min_bandwidth_hz > self.max_bandwidth_hz:
            raise ValueError('invalid RF operating range')

    def validate(self, frequency_hz, bandwidth_hz, sample_rate_hz):
        for name,value in [('frequency',frequency_hz),('bandwidth',bandwidth_hz),('sample_rate',sample_rate_hz)]:
            real(value,name,0)
        if not self.min_bandwidth_hz <= bandwidth_hz <= self.max_bandwidth_hz:
            raise ValueError('bandwidth outside RF front-end range')
        if not bandwidth_hz <= sample_rate_hz <= self.max_sample_rate_hz:
            raise ValueError('complex IQ sample rate must cover bandwidth and fit converter limits')
        if not (self.min_frequency_hz <= frequency_hz-bandwidth_hz/2 and
                frequency_hz+bandwidth_hz/2 <= self.max_frequency_hz):
            raise ValueError('occupied RF band outside front-end frequency range')


@dataclass(frozen=True)
class LOTunePlan:
    frequency_hz: float
    bandwidth_hz: float
    sample_rate_hz: float
    setup_ms: float
    ready_at_ms: float

    def apply_to_profile(self, profile):
        """Keep LO tuning separate from cold-start time, but charge both in planning."""
        return replace(profile, tuning_setup_ms=self.setup_ms)


class LOTuningController:
    def __init__(self, operating_range=None, base_lock_ms=1., slew_hz_per_ms=100e6,
                 bandwidth_switch_ms=.5):
        self.operating_range=operating_range or RFOperatingRange()
        self.base_lock_ms=real(base_lock_ms,'base_lock_ms',0)
        self.slew_hz_per_ms=real(slew_hz_per_ms,'slew_hz_per_ms',0)
        if self.slew_hz_per_ms == 0: raise ValueError('slew rate must be positive')
        self.bandwidth_switch_ms=real(bandwidth_switch_ms,'bandwidth_switch_ms',0)
        self.current=None
        self._lock=RLock()

    def preview(self, frequency_hz, bandwidth_hz, sample_rate_hz, now_ms=0):
        self.operating_range.validate(frequency_hz,bandwidth_hz,sample_rate_hz)
        real(now_ms,'now_ms',0)
        with self._lock:
            old=self.current
            if old is not None and now_ms < old.ready_at_ms:
                raise RuntimeError('LO is still tuning')
            changed=old is None or (frequency_hz,bandwidth_hz,sample_rate_hz)!=(old.frequency_hz,old.bandwidth_hz,old.sample_rate_hz)
            cost=0.
            if changed:
                cost=self.base_lock_ms
                if old:
                    cost+=abs(frequency_hz-old.frequency_hz)/self.slew_hz_per_ms
                    if (bandwidth_hz,sample_rate_hz)!=(old.bandwidth_hz,old.sample_rate_hz):
                        cost+=self.bandwidth_switch_ms
            return LOTunePlan(frequency_hz,bandwidth_hz,sample_rate_hz,cost,now_ms+cost)

    def tune(self, *args, **kwargs):
        with self._lock:
            self.current=self.preview(*args,**kwargs)
            return self.current

    def require_ready(self, now_ms):
        real(now_ms,'now_ms',0)
        with self._lock:
            if self.current is None or now_ms < self.current.ready_at_ms:
                raise RuntimeError('LO not locked')
            return self.current
