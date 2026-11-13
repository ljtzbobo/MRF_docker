"""Bounded wall-clock observations, separate from synthetic processing profiles."""
from collections import OrderedDict, deque
from hashlib import sha256
import json
from threading import Lock


class ActionCosts:
    def __init__(self, max_keys=256, samples_per_key=64):
        self.values = OrderedDict()
        self.max_keys, self.samples_per_key = max_keys, samples_per_key
        self.lock = Lock()

    @staticmethod
    def key(identity, implementation, host, configuration):
        digest = sha256(json.dumps(configuration, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:16]
        return identity, implementation, host, digest

    def observe(self, key, action, milliseconds):
        with self.lock:
            pair = key, action
            samples = self.values.setdefault(pair, deque(maxlen=self.samples_per_key))
            samples.append(max(0.0, milliseconds))
            self.values.move_to_end(pair)
            while len(self.values) > self.max_keys:
                self.values.popitem(last=False)

    def estimate(self, key, action, fallback):
        with self.lock:
            samples = self.values.get((key, action))
            if samples:
                return max(samples), 'observed_recent_max'
            if action in ('active_reuse', 'warm_reuse'):
                # Returning an instance measures the same reconstruct + health operations.
                samples = self.values.get((key, 'reset'))
                if samples:
                    return max(samples), 'observed_reset_recent_max'
            return fallback, 'profile_setup_fallback'

    def snapshot(self):
        with self.lock:
            return [{'runtime_identity': key[0], 'implementation_id': key[1],
                     'processing_node_id': key[2], 'configuration_hash': key[3],
                     'action': action, 'sample_count': len(samples),
                     'max_ms': max(samples), 'mean_ms': sum(samples)/len(samples), 'latest_ms': samples[-1]}
                    for (key, action), samples in self.values.items()]
