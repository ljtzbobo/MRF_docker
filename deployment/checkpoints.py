"""Docker-native, same-container CRIU checkpoints with a durable manifest.

Snapshot files belong to the Docker daemon. This database contains metadata,
not Python pickles or simulated process images. Only clean idle workers are saved.
"""
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from time import time
from uuid import uuid4

from .docker_runtime import ContainerHandle, DockerRuntimeError, DockerCleanupError


class DockerCheckpoints:
    def __init__(self, runtime, path):
        self.runtime = runtime
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS checkpoints (id TEXT PRIMARY KEY, unit TEXT UNIQUE, status TEXT NOT NULL, token TEXT, manifest TEXT NOT NULL, error TEXT)')

    def connect(self):
        db=sqlite3.connect(self.path,timeout=10)
        db.row_factory=sqlite3.Row
        return db

    def environment(self):
        info=json.loads(self.runtime._docker('info','--format','{{json .}}'))
        if info.get('OSType')!='linux' or not info.get('ExperimentalBuild'):
            raise DockerRuntimeError('CRIU requires a Linux Docker daemon with experimental checkpoint support')
        return {key:info.get(key) for key in ('ID','KernelVersion','Architecture','ServerVersion')}

    @staticmethod
    def configuration(a,node,image):
        raw=dict(cid=a.cid,implementation_id=a.implementation_id,processing_node_id=a.processing_node_id,
            processor=a.processor,demand=asdict(a.demand),image=image,
            node={key:node[key] for key in ('cid','parameters','inputs','outputs')},worker_protocol=1)
        return raw

    @staticmethod
    def fingerprint(config):
        return sha256(json.dumps(config,sort_keys=True,separators=(',',':')).encode()).hexdigest()

    def save(self,handle,allocation,node):
        env=self.environment()
        image=self.runtime.pool_identity(allocation.cid)
        actual=json.loads(self.runtime._docker('inspect',handle.container_id))[0]
        if (actual['Image']!=image or actual['Config'].get('Labels',{}).get('mrf.instance_id')!=handle.unit_id):
            raise DockerRuntimeError('checkpoint container identity differs from pool binding')
        self.runtime.verify_limits(actual,allocation)
        self.runtime.reset(handle,allocation,node)
        self.runtime.health(handle)
        with self.connect() as db:
            previous=db.execute('SELECT * FROM checkpoints WHERE unit=?',(handle.unit_id,)).fetchone()
            if previous is not None:
                if previous['status']!='consumed':raise DockerRuntimeError('unit already has a live checkpoint record')
                self.runtime._docker('checkpoint','rm',handle.container_id,previous['id'])
                db.execute('DELETE FROM checkpoints WHERE id=?',(previous['id'],))
        identifier='mrfcp-'+uuid4().hex
        config=self.configuration(allocation,node,image)
        manifest=dict(id=identifier,unit_id=handle.unit_id,container_id=handle.container_id,
            endpoint=handle.endpoint,environment=env,configuration=config,fingerprint=self.fingerprint(config),
            created_at=time(),kind='clean_warm_template',allocation=asdict(allocation))
        with self.connect() as db:
            db.execute('INSERT INTO checkpoints VALUES (?,?,?,NULL,?,NULL)',
                (identifier,handle.unit_id,'creating',json.dumps(manifest)))
        try:
            self.runtime._docker('checkpoint','create',handle.container_id,identifier)
            state=json.loads(self.runtime._docker('inspect',handle.container_id))[0]['State']
            if state.get('Running'):raise DockerRuntimeError('checkpoint did not confirm a stopped container')
            with self.connect() as db:db.execute('UPDATE checkpoints SET status=? WHERE id=?',('available',identifier))
            with self.runtime._handles_lock:self.runtime._handles.pop(handle.unit_id,None)
        except Exception as exc:
            self.invalidate(identifier,str(exc))
            raise
        return manifest

    def list(self):
        with self.connect() as db:
            return [dict(**json.loads(row['manifest']),status=row['status'],error=row['error'])
                    for row in db.execute('SELECT * FROM checkpoints ORDER BY id')]

    def compatible(self,node,image):
        return [row for row in self.list() if row['status']=='available'
                and row['configuration']['image']==image
                and row['configuration']['node']=={k:node[k] for k in ('cid','parameters','inputs','outputs')}]

    def claim(self,unit,token):
        with self.connect() as db:
            cursor=db.execute("UPDATE checkpoints SET status='claimed',token=? WHERE unit=? AND status='available'",(token,unit))
            if cursor.rowcount!=1:raise DockerRuntimeError('checkpoint is unavailable or already claimed')

    def unclaim(self,unit,token):
        with self.connect() as db:
            db.execute("UPDATE checkpoints SET status='available',token=NULL WHERE unit=? AND token=? AND status='claimed'",(unit,token))

    def invalidate(self,identifier,error):
        with self.connect() as db:
            db.execute("UPDATE checkpoints SET status='invalid',error=? WHERE id=?",(error,identifier))

    def restore(self,allocation,node):
        with self.connect() as db:
            row=db.execute('SELECT * FROM checkpoints WHERE unit=? AND token=? AND status=?',
                (allocation.container_unit_id,allocation.lease_id,'claimed')).fetchone()
        if row is None:raise DockerRuntimeError('no checkpoint claim for this lease')
        manifest=json.loads(row['manifest'])
        config=self.configuration(allocation,node,self.runtime.pool_identity(allocation.cid))
        handle=ContainerHandle(manifest['container_id'],manifest['endpoint'],allocation.container_unit_id)
        start_issued=False
        try:
            if manifest['environment']!=self.environment() or manifest['fingerprint']!=self.fingerprint(config):
                raise DockerRuntimeError('checkpoint environment, image or configuration is incompatible')
            actual=json.loads(self.runtime._docker('inspect',handle.container_id))[0]
            if (actual['State'].get('Running') or actual['Image']!=config['image'] or
                    actual['Config'].get('Labels',{}).get('mrf.instance_id')!=handle.unit_id):
                raise DockerCleanupError('checkpoint source ownership/state changed; manual reconciliation required')
            self.runtime.verify_limits(actual,allocation)
            start_issued=True
            self.runtime._docker('start','--checkpoint',manifest['id'],handle.container_id)
            port=self.runtime._docker('port',handle.container_id,'8765/tcp').splitlines()[0].rsplit(':',1)[-1]
            handle=ContainerHandle(handle.container_id,'http://127.0.0.1:'+str(int(port)),handle.unit_id)
            self.runtime.wait_ready(handle,allocation.cid)
            self.runtime.reset(handle,allocation,node)
            self.runtime.health(handle)
            with self.runtime._handles_lock:self.runtime._handles[handle.unit_id]=handle
            with self.connect() as db:db.execute("UPDATE checkpoints SET status='consumed' WHERE id=?",(manifest['id'],))
            return handle
        except Exception as exc:
            self.invalidate(manifest['id'],str(exc))
            if isinstance(exc,DockerCleanupError):raise
            if start_issued:
                try:self.runtime.stop(handle)
                except Exception as cleanup:
                    raise DockerCleanupError('restore cleanup is unconfirmed: '+str(cleanup)) from exc
            raise DockerRuntimeError('checkpoint restore failed: '+str(exc)) from exc

    def remove(self,identifier):
        row=next((r for r in self.list() if r['id']==identifier),None)
        if row is None:raise DockerRuntimeError('unknown checkpoint')
        if row['status'] not in ('available','invalid'):
            raise DockerRuntimeError('cannot remove a claimed or consumed checkpoint')
        # Never delete an object merely because its old manifest names it.
        try:actual=json.loads(self.runtime._docker('inspect',row['container_id']))[0]
        except DockerRuntimeError as exc:
            if not any(message in str(exc) for message in ('No such object','No such container')):raise
            actual=None  # Daemon confirmed absence, rather than an unavailable daemon.
        if actual is not None:
            if (actual['State'].get('Running') or actual['Image']!=row['configuration']['image'] or
                    actual['Config'].get('Labels',{}).get('mrf.instance_id')!=row['unit_id']):
                raise DockerRuntimeError('checkpoint source identity changed or is running')
            self.runtime.stop(ContainerHandle(row['container_id'],row['endpoint'],row['unit_id']))
        with self.connect() as db:db.execute('DELETE FROM checkpoints WHERE id=?',(identifier,))
