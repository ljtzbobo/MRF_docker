from dataclasses import replace
import json
from pathlib import Path
import tempfile
from threading import Lock
import unittest

from deployment.checkpoints import DockerCheckpoints
from deployment.docker_runtime import ContainerHandle, DockerRuntimeError, DockerCleanupError
from resources.measurements import PhysicalUsage
from resources.placement import PlacementAllocation


class CheckpointRuntime:
    def __init__(self):
        self.running=True; self.owner='unit'; self.engine='engine'; self.fail_start=False
        self.removed=False; self._handles={}; self._handles_lock=Lock()
    def _docker(self,*args):
        if args[0]=='info':return json.dumps(dict(ID=self.engine,OSType='linux',ExperimentalBuild=True))
        if args[0]=='inspect':return json.dumps([dict(Image='sha256:image',Config=dict(Labels={'mrf.instance_id':self.owner}),State=dict(Running=self.running))])
        if args[:2]==('checkpoint','create'):self.running=False
        if args[0]=='start':
            if self.fail_start:raise DockerRuntimeError('native restore failed')
            self.running=True
        if args[0]=='port':return '127.0.0.1:12345'
        return ''
    def pool_identity(self,cid):return 'sha256:image'
    def reset(self,*args):pass
    def health(self,*args):pass
    def wait_ready(self,*args):pass
    def verify_limits(self,*args):pass
    def stop(self,handle):self.removed=True; self.running=False


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.runtime=CheckpointRuntime()
        self.store=DockerCheckpoints(self.runtime,Path(self.folder.name)/'snapshots.sqlite')
        self.node=dict(id='ddc',cid='C03',parameters={'mode':'ddc'},inputs={'in':'iq/complex'},outputs={'out':'iq/complex'})
        self.a=PlacementAllocation('ddc','C03','C03-cpu','unit','cpu','cpu',PhysicalUsage(1000,256*2**20),1,1,runtime_identity='sha256:image',lease_id='lease')
        self.handle=ContainerHandle('container','http://127.0.0.1:12345','unit')
    def tearDown(self):self.folder.cleanup()
    def save(self):return self.store.save(self.handle,self.a,self.node)
    def test_roundtrip_claim_is_exclusive_and_consumed(self):
        self.save();self.assertFalse(self.runtime.running)
        self.store.claim('unit','lease')
        with self.assertRaises(DockerRuntimeError):self.store.claim('unit','other')
        self.assertEqual(self.store.restore(self.a,self.node).unit_id,'unit')
        self.assertEqual(self.store.list()[0]['status'],'consumed')
    def test_drift_invalidates_without_deleting_source(self):
        self.save();self.store.claim('unit','lease');self.runtime.engine='changed'
        with self.assertRaises(DockerRuntimeError):self.store.restore(self.a,self.node)
        self.assertFalse(self.runtime.removed)
        self.assertEqual(self.store.list()[0]['status'],'invalid')
    def test_wrong_lease_cannot_restore(self):
        self.save();self.store.claim('unit','lease')
        with self.assertRaises(DockerRuntimeError):self.store.restore(replace(self.a,lease_id='stale'),self.node)
        self.assertEqual(self.store.list()[0]['status'],'claimed')
    def test_unexpected_running_source_requires_reconciliation(self):
        self.save();self.store.claim('unit','lease');self.runtime.running=True
        with self.assertRaises(DockerCleanupError):self.store.restore(self.a,self.node)
        self.assertFalse(self.runtime.removed)
    def test_native_failure_cleans_only_verified_source(self):
        self.save();self.store.claim('unit','lease');self.runtime.fail_start=True
        with self.assertRaises(DockerRuntimeError):self.store.restore(self.a,self.node)
        self.assertTrue(self.runtime.removed)
        self.assertEqual(self.store.list()[0]['status'],'invalid')
    def test_parameter_change_excludes_candidate(self):
        self.save()
        self.assertEqual(len(self.store.compatible(self.node,'sha256:image')),1)
        self.assertFalse(self.store.compatible({**self.node,'parameters':{'mode':'duc'}},'sha256:image'))
    def test_foreign_container_is_never_deleted(self):
        manifest=self.save();self.runtime.owner='somebody-else'
        with self.assertRaises(DockerRuntimeError):self.store.remove(manifest['id'])
        self.assertFalse(self.runtime.removed)
