from copy import deepcopy
from hashlib import sha512
import io
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch
import wave

import numpy as np

from datasets.catalog import DatasetCatalog,DatasetError,inside
from datasets.inventory import scan
from datasets.generator import generate,compatibility
from datasets.execution import DatasetFrames,evaluate
from datasets.adapters import open_reader
from datasets.adapters.sigmf import SigMFReader,utc_timestamp
from deployment.simulation import load_simulation_manager
from examples.heterogeneous_simulation_demo import CONFIG
from task_repr import parse_external_task,refine_business_task
from matching import match_task

ROOT=Path(__file__).resolve().parents[1]


class DatasetFixture(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        shutil.copytree(ROOT/'datasets/scenarios',self.root/'scenarios')
        pairs=np.column_stack((np.arange(16384)%30000,-np.arange(16384)%30000)).astype('<i2')
        (self.root/'capture.sigmf-data').write_bytes(pairs.tobytes())
        self.meta=dict(global_={},captures=[{'core:sample_start':0,'core:frequency':1090e6,
            'core:datetime':'2026-08-27T08:36:43.1544015625Z'}],annotations=[])
        self.meta['global']=dict({'core:datatype':'ci16_le','core:sample_rate':8e6,'core:version':'1.2.6',
            'core:sha512':sha512(pairs.tobytes()).hexdigest()});self.meta.pop('global_')
        self.write_meta();self.catalog=DatasetCatalog(self.root);self.index()

    def tearDown(self):self.tmp.cleanup()
    def write_meta(self):(self.root/'capture.sigmf-meta').write_text(json.dumps(self.meta),encoding='utf-8')
    def index(self):
        scan(self.root,hash_files=True)
        self.record=next(r for r in self.catalog.read()['records'] if r['adapter']=='sigmf')
    def task(self,**kwargs):return generate(self.catalog,'adsb_spectrum',self.record['record_id'],kwargs)['task']

    def test_metadata_only_external_task_and_deterministic_variants(self):
        with patch.object(SigMFReader,'read',side_effect=AssertionError('generation must not read samples')):
            task=self.task();again=self.task();changed=self.task(priority=2)
        self.assertEqual(task,again);self.assertNotEqual(task['task_id'],changed['task_id'])
        self.assertFalse({'S','Q','R','G','X'} & task.keys())
        self.assertNotIn('data_base64',json.dumps(task))
        self.assertEqual(task['request']['processing']['input_sample_rate_mhz'],8)
        self.assertEqual(parse_external_task(task)['S']['data_input']['dataset_binding']['source_sha256'],self.record['sha256'])
        self.assertTrue(compatibility(task)['executable_components'])

    def test_window_conversion_and_lazy_frame_boundaries(self):
        frames=DatasetFrames(self.catalog,self.task())
        self.assertEqual(len(frames),3)
        values=list(frames)
        first=next(iter(values[0].values()));second=next(iter(values[1].values()))
        self.assertEqual(first.sample_rate,8e6);self.assertEqual(first.center_frequency,1090e6)
        self.assertAlmostEqual(first.samples[1].real,1/32768)
        self.assertEqual(second.metadata['sample_start'],4096)
        self.assertAlmostEqual(second.timestamp-first.timestamp,4096/8e6,places=6)
        self.assertFalse(first.samples.flags.writeable)

    def test_invalid_experiments_rejected(self):
        for values in (dict(frame_count=0),dict(frame_count=True),dict(frame_count=1025),dict(sample_start=16000),
                dict(nco_frequency_hz=5e6),dict(nco_frequency_hz='1'),dict(overlap=1),dict(decimation=3),
                dict(priority=99),dict(unknown=1),dict(frame_length=512),dict(deadline_after_ms=0)):
            with self.subTest(values=values),self.assertRaises((DatasetError,ValueError)):
                self.task(**values)

    def test_checksum_mismatch_is_not_ready(self):
        self.meta['global']['core:sha512']='0'*128;self.write_meta();self.index()
        self.assertEqual(self.record['status'],'integrity_error')
        with self.assertRaises(DatasetError):self.task()

    def test_nonzero_sigmf_offset_is_not_a_byte_offset(self):
        self.meta['global']['core:offset']=100
        self.meta['captures'][0]['core:sample_start']=100;self.write_meta();self.index()
        packet=open_reader(self.catalog,self.record).read(100,2)
        self.assertAlmostEqual(packet.samples[1].real,1/32768)
        self.assertEqual(self.task()['data_input']['dataset_binding']['sample_start'],100)

    def test_capture_frequency_is_not_inherited_and_crossing_is_rejected(self):
        self.meta['captures'].append({'core:sample_start':8192,'core:frequency':1089e6})
        self.write_meta();self.index()
        with self.assertRaises(DatasetError):self.task()
        task=self.task(sample_start=8192,frame_count=1)
        self.assertEqual(task['data_input']['center_frequency_hz'],1089e6)

    def test_source_change_and_path_escape_rejected(self):
        for ref in ('../outside','C:/outside','/tmp/outside','a\\b'):
            with self.assertRaises(DatasetError):inside(self.root,ref)
        with (self.root/'capture.sigmf-data').open('ab') as file:file.write(b'xxxx')
        with self.assertRaises(DatasetError):self.task()

    def test_edit_between_frames_is_detected(self):
        frames=iter(DatasetFrames(self.catalog,self.task()));next(frames)
        with (self.root/'capture.sigmf-data').open('ab') as file:file.write(b'xxxx')
        with self.assertRaises(DatasetError):next(frames)

    def test_radar_description_keeps_prf_separate_from_adc_rate(self):
        try:from scipy.io import savemat
        except ImportError:self.skipTest('optional scipy not installed')
        folder=self.root/'data1.7z/data1';folder.mkdir(parents=True)
        savemat(folder/'data1.mat',{'radar_pulse_squence':np.zeros((319,640))})
        (self.root/'sources.json').write_text(json.dumps({'data1.7z/data1/data1.mat':{'dataset_id':'dim-aircraft-radar'}}))
        scan(self.root,hash_files=True)
        record=next(r for r in self.catalog.read()['records'] if r['adapter']=='radar_reference')
        task=generate(self.catalog,'radar_search',record['record_id'])['task']
        self.assertEqual(task['data_input']['pulse_repetition_hz'],32000)
        self.assertIsNone(task['data_input']['sample_rate_hz'])
        self.assertEqual(task['data_input']['dataset_binding']['index_unit'],'slow_time_pulse')
        self.assertFalse(compatibility(task)['executable_components'])
        with self.assertRaises(DatasetError):DatasetFrames(self.catalog,task)

    def test_nanosecond_capture_time_supported(self):
        self.assertAlmostEqual(utc_timestamp('2026-08-27T08:36:43.1544015625Z')%1,.1544015625,places=6)

    def test_real_reader_pipeline_under_modeled_resources(self):
        task=self.task();refined=refine_business_task(parse_external_task(task));manager=load_simulation_manager(CONFIG)
        try:
            result=manager.execute(refined.task,match_task(refined),DatasetFrames(self.catalog,task),keep_warm=True)
            self.assertEqual(len(result.frame_outputs),3)
            self.assertTrue(all(r['finite'] for r in evaluate(result)['outputs']))
            self.assertIsNone(evaluate(result)['ground_truth_metrics'])
            self.assertFalse(manager.scheduler.placement_snapshot()['active_tasks'])
        finally:manager.pool.close()

    def audio(self):
        buffer=io.BytesIO()
        with wave.open(buffer,'wb') as file:
            file.setnchannels(1);file.setsampwidth(2);file.setframerate(16000)
            file.writeframes(np.arange(1600,dtype='<i2').tobytes())
        return buffer.getvalue()

    def test_tar_audio_is_a_separate_data_type_and_business_task(self):
        audio=self.audio()
        with tarfile.open(self.root/'atco.tgz','w:gz') as file:
            info=tarfile.TarInfo('atco/example.wav');info.size=len(audio);file.addfile(info,io.BytesIO(audio))
        scan(self.root,hash_files=True)
        record=next(r for r in self.catalog.read()['records'] if r['adapter']=='tar_wav')
        with self.assertRaises(DatasetError):generate(self.catalog,'adsb_spectrum',record['record_id'])
        task=generate(self.catalog,'atco_audio_review',record['record_id'])['task']
        self.assertEqual(task['data_input']['format'],'audio/pcm')
        self.assertFalse(compatibility(task)['executable_components'])
        packet=open_reader(self.catalog,record).read(10,20)
        self.assertAlmostEqual(packet.samples[0],10/32768)
        self.assertFalse((self.root/'atco').exists())
        with self.assertRaises(DatasetError):DatasetFrames(self.catalog,task)

    def test_iridium_slices_do_not_concatenate_observations_or_leak_labels(self):
        try:import h5py
        except ImportError:self.skipTest('optional h5py not installed')
        path=self.root/'data_pluto1.mat'
        with h5py.File(path,'w',userblock_size=512) as source:
            source['QPSK']=np.ones((2,3000,2),dtype='float32')
            source['BPSK']=np.ones((2,461,2),dtype='float32')
            source['pilot']=np.ones((2,2496,2),dtype='float32')
            source['satlabel']=np.array([[1],[2]],dtype='uint8')
            source['snr']=np.zeros((2,1),dtype='float32');source['Doppler']=np.zeros((2,1),dtype='float32')
        with path.open('r+b') as file:file.write(b'MATLAB 7.3 MAT-file'.ljust(128,b' '))
        (self.root/'sources.json').write_text(json.dumps({'data_pluto1.mat':dict(dataset_id='iridiumrf-mr',description_url='https://example.test/source')}))
        scan(self.root,hash_files=True)
        record=next(r for r in self.catalog.read()['records'] if r.get('variable')=='QPSK')
        reader=open_reader(self.catalog,record,1);packet=reader.read(0,2048)
        np.testing.assert_allclose(packet.samples,1+1j)
        self.assertNotIn('satellite_label',packet.metadata)
        self.assertEqual(reader.reference()['satellite_label'],2)
        with self.assertRaises(DatasetError):reader.read(0,3001)
        with self.assertRaises(DatasetError):open_reader(self.catalog,record,2)


class DatasetApiTests(unittest.TestCase):
    setUp=DatasetFixture.setUp
    tearDown=DatasetFixture.tearDown
    write_meta=DatasetFixture.write_meta
    index=DatasetFixture.index
    def test_api_generation_and_lazy_execution_progress(self):
        from fastapi.testclient import TestClient
        from backend.api.app import create_app
        from backend.services import TaskService
        from backend.repositories import JsonDocumentRepository
        manager=load_simulation_manager(CONFIG)
        service=TaskService(JsonDocumentRepository(ROOT/'examples/TaskDemos'),deployment=manager)
        with patch.dict('os.environ',{'MRF_DATASET_ROOT':str(self.root)}),TestClient(create_app(task_service=service)) as client:
            self.assertEqual(client.get('/api/v1/datasets').status_code,200)
            response=client.post('/api/v1/datasets/tasks',json=dict(scenario_id='adsb_spectrum',record_id=self.record['record_id']))
            self.assertEqual(response.status_code,200,response.text)
            task=response.json()['task']
            # Follow the UI order: generate -> represent -> match -> plan -> execute.
            represented=client.post('/api/v1/task-representations/external',json=task)
            self.assertEqual(represented.status_code,200,represented.text)
            normalized=represented.json()['task_representation']
            self.assertEqual(normalized['G']['generation_status'],'abstract_only_requires_refinement')
            self.assertEqual(normalized['G']['component_bindings'],[])
            restored=client.post('/api/v1/task-representations/normalized',json=normalized)
            self.assertEqual(restored.status_code,200,restored.text)
            self.assertEqual(restored.json()['task_representation'],normalized)
            matched=client.post('/api/v1/matches',json=normalized)
            self.assertEqual(matched.status_code,200,matched.text)
            self.assertEqual(matched.json()['status'],'MATCHED')
            plan=client.post('/api/v1/plans?keep_warm=true',json=task)
            self.assertEqual(plan.status_code,200,plan.text)
            self.assertEqual(plan.json()['status'],'FEASIBLE_ESTIMATE',plan.text)
            job=client.post('/api/v1/datasets/executions',json={'task':task,'keep_warm':True})
            self.assertEqual(job.status_code,200,job.text)
            identifier=job.json()['job_id'];deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                progress=client.get('/api/v1/executions/'+identifier).json()
                if progress['state'] in ('COMPLETED','FAILED'):break
                time.sleep(.01)
            self.assertEqual(progress['state'],'COMPLETED',progress.get('error'))
            self.assertEqual(progress['completed_frames'],3)
            self.assertEqual(client.get('/api/v1/executions/'+identifier+'/result').status_code,200)
            self.assertEqual(client.post('/api/v1/datasets/tasks',json={'scenario_id':'../oops','record_id':self.record['record_id']}).status_code,422)
