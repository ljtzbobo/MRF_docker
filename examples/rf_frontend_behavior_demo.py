"""TX / RX behavior chains on sampled IQ, without physical RF equipment."""
import json
from pathlib import Path
import numpy as np
from components.runtime import DataPacket
from components.implementations.runtime_registry import create_runtime_component
from components.implementations.rf.aperture import SharedAperture
from components.implementations.rf.tuning import LOTuningController


def run_chain(packet, configuration):
    report=[]
    for cid,parameters in configuration:
        component=create_runtime_component(cid,**parameters)
        packet=component.process(packet)
        report.append(dict(cid=cid,output_samples=packet.item_count,sample_rate=packet.sample_rate,
                           mean_square_v2=float(np.mean(abs(packet.samples)**2)),
                           model_metrics=component.metrics.component_specific_metrics))
    return packet,report


def main():
    rate=1_000_000;n=np.arange(4096)
    packet=DataPacket(.01*np.exp(2j*np.pi*50000*n/rate),rate,100e6,0,'time')
    tuner=LOTuningController()
    initial_tune=tuner.tune(100e6,200000,rate)
    tuner.require_ready(initial_tune.ready_at_ms)
    aperture=SharedAperture();aperture.acquire('demo-tx','tx')
    try:
        transmitted,tx=run_chain(packet,[('C47',dict(bits=14,interpolation=2)),
            ('C41',dict(bandwidth_hz=200000)),('C44',dict(offset_hz=100000,direction='up')),
            ('C45',dict(phase_diffusion_rad2_per_s=.01)),('C48',dict(gain_db=10)),
            ('C39',{}),('C38',{})])
    finally:aperture.release('demo-tx')
    # Offline loopback, not simultaneous TX/RX over a physical aperture or air path.
    aperture.acquire('demo-rx','rx')
    try:
        received,rx=run_chain(transmitted,[('C38',{}),('C39',{}),
            ('C41',dict(bandwidth_hz=500000)),('C42',dict(gain_db=10)),
            ('C43',dict(gain_db=-10)),('C44',dict(offset_hz=100000)),
            ('C49',dict(jitter_rms_s=1e-12,reference_frequency_hz=100e6)),
            ('C41',dict(bandwidth_hz=200000)),('C46',dict(bits=12,decimation=2))])
    finally:aperture.release('demo-rx')
    report=dict(simulation_only=True,representation='sampled equivalent baseband RMS volts',
                tx=tx,rx=rx,lo_setup_ms=initial_tune.setup_ms,
                final_samples=received.item_count,aperture_released=not aperture.snapshot())
    path=Path('outputs/rf-frontend-behavior.json');path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(path)


if __name__=='__main__':main()
