import json
from pathlib import Path
import unittest
import numpy as np

from components import default_component_catalog, default_capability_map, load_component_specs
from components.implementations.runtime_registry import create_runtime_component as create
from components.implementations.rf.aperture import SharedAperture
from components.implementations.rf.tuning import RFOperatingRange, LOTuningController
from components.runtime import DataPacket
from matching import match_task
from task_repr import represent_task
from task_executor import DAGExecutor
from examples.heterogeneous_simulation_demo import CONFIG, sample_task
from deployment.simulation import load_simulation_manager
from resources.measurements import MeasurementCatalog
from dataclasses import replace


def packet(x, fs=1e6, timestamp=0):
    return DataPacket(np.asarray(x,dtype=complex),fs,100e6,timestamp,'time')


class RFBehaviorTests(unittest.TestCase):
    def test_frequency_bandwidth_and_converter_limits(self):
        limits=RFOperatingRange()
        limits.validate(100e6,1e6,2e6)
        for args in [(30e6,1e6,2e6),(100e6,2e6,1e6),(6e9,1e6,2e6),(100e6,50e6,60e6),(100e6,1e6,100e6)]:
            with self.subTest(args=args),self.assertRaises(ValueError):limits.validate(*args)

    def test_lo_retune_cost_and_ready_gate(self):
        lo=LOTuningController(base_lock_ms=1,slew_hz_per_ms=100e6,bandwidth_switch_ms=.5)
        first=lo.tune(100e6,1e6,2e6)
        self.assertEqual(first.setup_ms,1)
        with self.assertRaises(RuntimeError):lo.require_ready(.9)
        with self.assertRaises(RuntimeError):lo.tune(200e6,1e6,2e6,now_ms=.9)
        lo.require_ready(1)
        self.assertEqual(lo.preview(100e6,1e6,2e6,now_ms=1).setup_ms,0)
        hop=lo.tune(300e6,2e6,4e6,now_ms=1)
        self.assertEqual(hop.setup_ms,3.5)
        self.assertEqual(hop.ready_at_ms,4.5)

    def test_tuning_cost_is_charged_by_every_placement_algorithm(self):
        _,task,match=sample_task()
        manager=load_simulation_manager(CONFIG)
        planner=manager.scheduler.placement_planner
        plan=LOTuningController(base_lock_ms=2000).preview(100e6,1e6,2e6)
        planner.measurements=MeasurementCatalog(tuple(plan.apply_to_profile(p) if p.cid=='C03' else p
                                                     for p in planner.measurements.profiles))
        for algorithm in ('exact','chain_dp','heft'):
            with self.subTest(algorithm=algorithm):
                result=manager.scheduler.preview_placement(task,match,algorithm=algorithm)
                self.assertNotEqual(result.status,'FEASIBLE_ESTIMATE')
                self.assertGreater(result.estimated_completion_ms,task.Q['deadline_ms'])

    def test_all_models_registered_and_explicitly_matchable(self):
        catalog, mapping, specs = default_component_catalog(), default_capability_map(), load_component_specs()
        for i in range(38,50):
            cid=f'C{i}'
            self.assertEqual(catalog.get(cid).cid,cid)
            self.assertIn(cid,specs)
            self.assertTrue(any(m.cid==cid for m in mapping.mappings))
            raw=json.loads(Path('examples/TaskDemos/voice_tx_task.json').read_text(encoding='utf-8'))
            fmt='iq/multichannel' if cid=='C50' else 'iq/complex'
            inputs={k:fmt for k in (('left','right') if cid=='C40' else ('in',))}
            capability=next(m.capabilities[0] for m in mapping.mappings if m.cid==cid)
            params={'gains_db':'[0, 3]'} if cid=='C50' else {}
            raw['graph']={'nodes':[dict(id='rf',capability=capability,inputs=inputs,outputs={'out':fmt},parameters=params,estimated_runtime_ms=1)],'edges':[]}
            result=match_task(raw)
            self.assertEqual(result.status,'MATCHED',result.to_dict())
            if cid=='C40': create(cid).process_inputs(dict(left=packet(np.ones(128)),right=packet(np.zeros(128))))
            elif cid=='C50': create(cid).process(packet(np.ones((2,128))))
            else: self.assertTrue(np.isfinite(create(cid).process(packet(np.ones(128)*.01)).samples).all())
        self.assertEqual(mapping.candidates_for('rf_receive'),())

    def test_dac_adc_dag_rates_and_lengths(self):
        raw=json.loads(Path('examples/TaskDemos/voice_tx_task.json').read_text(encoding='utf-8'))
        nodes=[dict(id='dac',capability='dac_sim',inputs={'in':'iq/complex'},outputs={'out':'iq/complex'},estimated_runtime_ms=1,parameters=dict(interpolation=2,sample_rate_mhz=1,input_block_length=32,output_block_length=64)),
               dict(id='adc',capability='adc_sim',inputs={'in':'iq/complex'},outputs={'out':'iq/complex'},estimated_runtime_ms=1,parameters=dict(decimation=2,sample_rate_mhz=2,input_block_length=64,output_block_length=32))]
        raw['graph']=dict(nodes=nodes,edges=[dict(source='dac',target='adc',source_port='out',target_port='in')])
        task=represent_task(raw);match=match_task(task)
        self.assertEqual(match.status,'MATCHED',match.to_dict())
        result=DAGExecutor.from_match(task,match).execute({'dac':packet(np.ones(32)*.125)})
        np.testing.assert_allclose(result.sink_outputs['adc'].samples,.125)
        self.assertEqual(result.sink_outputs['adc'].sample_rate,1e6)
        nodes[1]['parameters']['output_block_length']=64
        self.assertNotEqual(match_task(raw).status,'MATCHED')

    def test_antenna_half_power_and_switch_isolation(self):
        x=packet(np.ones(200))
        a=create('C38',angle_deg=45,beamwidth_deg=90).process(x).samples
        self.assertAlmostEqual(float(np.mean(abs(a)**2)),10**(-3/10))
        a=create('C39',enabled=False,isolation_db=60).process(x).samples
        np.testing.assert_allclose(a,.001)
        a=create('C40',insertion_loss_db=0,isolation_db=60).process_inputs(dict(left=x,right=x)).samples
        np.testing.assert_allclose(a,1.001)

    def test_lna_noise_power_matches_thermal_model(self):
        c=create('C42',gain_db=0,noise_figure_db=3,noise_bandwidth_hz=100000,seed=7)
        y=c.process(packet(np.zeros(120000))).samples
        expected=1.380649e-23*290*100000*50*(10**.3-1)
        self.assertAlmostEqual(float(np.mean(abs(y)**2))/expected,1,delta=.02)
        np.testing.assert_allclose(y,c.process(packet(np.zeros(120000))).samples)

    def test_bandpass_rejects_out_of_band_tone(self):
        n=np.arange(8192)
        x=np.exp(2j*np.pi*.05*n)+np.exp(2j*np.pi*.35*n)
        y=create('C41',bandwidth_hz=200000,taps=129).process(packet(x)).samples[256:]
        ratio=abs(np.mean(y*np.exp(-2j*np.pi*.35*n[256:])))/abs(np.mean(y*np.exp(-2j*np.pi*.05*n[256:])))
        self.assertLess(ratio,.01)

    def test_quantization_overload_rates_and_hold(self):
        x=np.linspace(-.8,.8,1000)*(1+1j)
        y=create('C46',bits=8).process(packet(x)).samples
        self.assertLessEqual(np.max(abs((y-x).real)),1/256+1e-12)
        out=create('C46',bits=8,decimation=2).process(packet(x))
        self.assertEqual(out.sample_rate,500000)
        self.assertEqual(len(out.samples),500)
        dac=create('C47',bits=8,interpolation=3).process(packet(y))
        self.assertEqual(dac.sample_rate,3000000)
        np.testing.assert_array_equal(dac.samples,np.repeat(y,3))
        adc=create('C46',bits=4)
        clipped=adc.process(packet([10,-10])).samples
        np.testing.assert_allclose(clipped.real,[.875,-1])
        self.assertEqual(adc.metrics.component_specific_metrics['clipped_samples'],2)

    def test_pa_compression_and_iq_mixer(self):
        x=packet(np.array([.01,1.,100.]))
        y=create('C48',gain_db=0,saturation_v=1).process(x).samples
        self.assertAlmostEqual(abs(y[0]),.01,places=6)
        self.assertLessEqual(np.max(abs(y)),1)
        n=np.arange(2048)
        y=create('C44',offset_hz=100000).process(packet(np.exp(2j*np.pi*.1*n))).samples
        np.testing.assert_allclose(y,1,atol=1e-12)

    def test_stream_state_matches_whole_block(self):
        x=np.exp(2j*np.pi*.03*np.arange(1024))
        for cid,params in [('C39',dict(settling_samples=17)),('C41',{}),('C42',{}),
                           ('C44',dict(offset_hz=10000)),('C45',dict(phase_diffusion_rad2_per_s=1)),
                           ('C46',dict(decimation=3)),('C47',dict(interpolation=2)),('C49',dict(jitter_rms_s=1e-10))]:
            with self.subTest(cid=cid):
                full=create(cid,**params).process(packet(x)).samples
                c=create(cid,**params)
                chunks=[]
                for start,end in [(0,173),(173,777),(777,1024)]:
                    chunks.extend(p.samples for p in c.push(packet(x[start:end],timestamp=start/1e6)))
                np.testing.assert_allclose(np.concatenate(chunks),full,atol=1e-12)

    def test_shared_aperture_tdd_leases(self):
        a=SharedAperture();a.acquire('r1','rx');a.acquire('r2','rx')
        with self.assertRaises(RuntimeError):a.acquire('t','tx')
        a.release('r1');a.release('r2');a.acquire('t','tx')
        with self.assertRaises(RuntimeError):a.acquire('r','rx')
        a.release('t');self.assertEqual(a.snapshot(),{})

    def test_explicit_rf_task_matches_and_bad_parameters_rejected(self):
        raw=json.loads(Path('examples/TaskDemos/voice_tx_task.json').read_text(encoding='utf-8'))
        raw['graph']={'nodes':[dict(id='adc',capability='adc_sim',inputs={'in':'iq/complex'},outputs={'out':'iq/complex'},estimated_runtime_ms=1,parameters={'bits':10})],'edges':[]}
        self.assertEqual(match_task(raw).status,'MATCHED')
        raw['graph']['nodes'][0]['parameters']['unknown_gain']=5
        self.assertNotEqual(match_task(raw).status,'MATCHED')
        for cid,params in [('C46',dict(bits=100)),('C42',dict(noise_figure_db=-1)),('C43',dict(gain_db=100))]:
            with self.subTest(cid=cid), self.assertRaises(ValueError):create(cid,**params)


if __name__=='__main__':unittest.main()
