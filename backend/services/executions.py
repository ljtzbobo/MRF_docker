"""Bounded in-process execution jobs with real node/frame completion events."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import RLock
from time import time
from uuid import uuid4


class ExecutionJobs:
    def __init__(self, service, max_jobs=32):
        self.service = service
        self.max_jobs = max_jobs
        self.jobs = {}
        self.lock = RLock()
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='mrf-execution')

    def submit(self, task, frames, *, keep_warm=False):
        with self.lock:
            while len(self.jobs) >= self.max_jobs:
                finished = next((k for k,v in self.jobs.items() if v['state'] in ('COMPLETED','FAILED')), None)
                if finished is None: raise RuntimeError('execution queue is full')
                del self.jobs[finished]
            job_id = uuid4().hex
            self.jobs[job_id] = dict(job_id=job_id, task_id=task.get('task_id'), state='QUEUED',
                stage='queued', total_frames=len(frames), completed_frames=0,
                completed_calls=0, total_calls=0, current_node=None, nodes={}, events=[],
                plan=None, error=None, resources_released=False, created_at=time(), result=None, waiting_since=None)
            self.executor.submit(self._run, job_id, deepcopy(task), frames, keep_warm)
            return self.snapshot(job_id)

    def _event(self, job_id, event):
        with self.lock:
            job = self.jobs[job_id]
            kind, node = event['event'], event.get('node_id')
            job['stage'] = kind
            if kind == 'waiting_for_instance': job['waiting_since'] = time()
            elif kind in ('planned','queue_fallback'): job['waiting_since'] = None
            job['events'].append(dict(event, at=time()))
            job['events'] = job['events'][-256:]
            if kind in ('planned','waiting_for_instance','instance_acquired','rebound') and 'plan' in event:
                job['plan'] = event['plan']
                job['total_frames'] = event.get('total_frames',job['total_frames'])
                job['total_calls'] = len(event['plan']['allocations']) * job['total_frames']
                for a in event['plan']['allocations']:
                    job['nodes'].setdefault(a['node_id'],dict(state='WAITING',completed_frames=0))
            if node:
                info = job['nodes'].setdefault(node, dict(state='WAITING', completed_frames=0))
                info['state'] = {'starting_node':'STARTING', 'node_ready':'READY',
                    'processing_node':'RUNNING', 'node_completed':'COMPLETED', 'node_failed':'FAILED'}.get(kind, info['state'])
                job['current_node'] = node if kind in ('starting_node','processing_node') else None
                if kind == 'node_completed':
                    info['completed_frames'] += 1
                    job['completed_calls'] += 1
            if kind == 'frame_completed': job['completed_frames'] += 1
            if kind == 'resources_released': job['resources_released'] = True
            # Plans are retained separately; avoid duplicating large payloads in event history.
            job['events'][-1].pop('plan', None)

    def _run(self, job_id, task, frames, keep_warm):
        with self.lock: self.jobs[job_id]['state'] = 'RUNNING'
        try:
            result = self.service.execute_deployed(task, frames, progress=lambda e:self._event(job_id,e),
                **({'keep_warm': True} if keep_warm else {}))
            with self.lock:
                self.jobs[job_id].update(state='COMPLETED', stage='completed', result=result,
                                        plan=result.plan.to_dict(), finished_at=time())
        except Exception as exc:
            with self.lock:
                self.jobs[job_id].update(state='FAILED', stage='failed', error=str(exc), finished_at=time())

    def snapshot(self, job_id):
        with self.lock:
            if job_id not in self.jobs: raise KeyError(job_id)
            snapshot = deepcopy({k:v for k,v in self.jobs[job_id].items() if k != 'result'})
            snapshot['waiting_ms'] = max(0,(time()-snapshot['waiting_since'])*1000) if snapshot['waiting_since'] else 0
            return snapshot

    def result(self, job_id):
        with self.lock:
            if job_id not in self.jobs: raise KeyError(job_id)
            return self.jobs[job_id]['result']

    def close(self):
        self.executor.shutdown(wait=True)
