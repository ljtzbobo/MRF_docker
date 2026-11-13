"""Thread-safe logical TDD shared-aperture leases, not electromagnetic simulation."""
from threading import RLock


class SharedAperture:
    def __init__(self):
        self._lock = RLock()
        self._leases = {}

    def acquire(self, task_id, direction):
        if not task_id or direction not in ('rx','tx'): raise ValueError('task and rx/tx direction required')
        with self._lock:
            if task_id in self._leases: raise ValueError('task already holds aperture')
            if self._leases and (direction == 'tx' or 'tx' in self._leases.values()):
                raise RuntimeError('TDD shared aperture busy')
            self._leases[task_id] = direction

    def release(self, task_id):
        with self._lock:
            if task_id not in self._leases: raise ValueError('unknown aperture lease')
            del self._leases[task_id]

    def snapshot(self):
        with self._lock: return dict(self._leases)
