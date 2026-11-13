"""Sample-domain RF behavior models. Complex samples are RMS-envelope volts.

These operators never open a device or radiate. Analog effects are approximated
on an already sampled envelope; see RF_BEHAVIORAL_MODELS.md for equations/limits.
"""
import numpy as np

from ...runtime import RuntimePort, DataPacket
from ...runtime.blocks import BlockComponent
from .._signal import fir, lowpass, real, integer
from ..common.iq_merge_component import IQMergeComponent


class RFBehavior(BlockComponent):
    parameter_defaults = {}

    def __init__(self, **parameters):
        super().__init__(**parameters)

    def _validate_config(self, **parameters):
        if set(parameters) - set(self.parameter_defaults):
            raise ValueError('unknown RF model parameter')
        p = dict(self.parameter_defaults, **parameters)
        for key, value in p.items():
            if type(self.parameter_defaults[key]) in (int, float):
                real(value, key)
        self._validate(p)
        return p

    def _validate(self, p):
        pass

    def _setup(self):
        self.metadata_key = self.cid + '_rf_model'
        self.input_port = RuntimePort('in', 'input', ('time',), dimensions=(1,))
        self.output_port = RuntimePort('out', 'output', ('time',), dimensions=(1,))

    def _normal(self, count, width=1):
        rng = np.random.default_rng(self._parameters.get('seed', 0))
        if 'rng' in self._state: rng.bit_generator.state = self._state['rng']
        values = rng.standard_normal((count, width))
        self._state['rng'] = rng.bit_generator.state
        return values

    def _index(self, count):
        start = self._state.get('count', 0)
        self._state['count'] = start + count
        return start + np.arange(count)


def positive(value, name):
    if real(value, name) <= 0: raise ValueError(name + ' must be positive')


def gain(value):
    if not -120 <= real(value, 'gain_db') <= 120: raise ValueError('gain_db out of range')
    return 10 ** (value / 20)


class AntennaApertureComponent(RFBehavior):
    cid = 'C38'
    parameter_defaults = dict(gain_dbi=0., efficiency=1., angle_deg=0., boresight_deg=0., beamwidth_deg=90.)

    def _validate(self, p):
        gain(p['gain_dbi'])
        positive(p['beamwidth_deg'], 'beamwidth_deg')
        if not 0 < p['efficiency'] <= 1: raise ValueError('efficiency must be in (0,1]')

    def _execute(self, x):
        p = self._parameters
        angle = (p['angle_deg'] - p['boresight_deg'] + 180) % 360 - 180
        attenuation_db = 12 * (angle / p['beamwidth_deg']) ** 2
        self._business = dict(pattern_loss_db=float(attenuation_db), simulation_only=True)
        return x * gain(p['gain_dbi']) * np.sqrt(p['efficiency']) * 10 ** (-attenuation_db / 20)


class RFSwitchComponent(RFBehavior):
    cid = 'C39'
    parameter_defaults = dict(enabled=True, insertion_loss_db=1., isolation_db=60., settling_samples=0)

    def _validate(self, p):
        if type(p['enabled']) is not bool: raise ValueError('enabled must be boolean')
        for k in ('insertion_loss_db', 'isolation_db'): real(p[k], k, 0)
        integer(p['settling_samples'], 'settling_samples', 0)

    def _execute(self, x):
        p = self._parameters
        index = self._index(len(x))
        passed = p['enabled'] & (index >= p['settling_samples'])
        self._business = dict(passed_samples=int(np.count_nonzero(passed)), simulation_only=True)
        return x * np.where(passed, 10 ** (-p['insertion_loss_db']/20), 10 ** (-p['isolation_db']/20))


class DuplexerComponent(IQMergeComponent):
    """left = wanted RX, right = TX leakage at the same envelope reference."""
    cid = 'C40'

    def __init__(self, insertion_loss_db=1., isolation_db=60.):
        BlockComponent.__init__(self, insertion_loss_db=insertion_loss_db,isolation_db=isolation_db)

    def _validate_config(self, insertion_loss_db=1., isolation_db=60.):
        real(insertion_loss_db, 'insertion_loss_db', 0)
        real(isolation_db, 'isolation_db', 0)
        return dict(insertion_loss_db=insertion_loss_db,isolation_db=isolation_db)

    def process_inputs(self, packets):
        def action():
            if set(packets) != {'left','right'}: raise ValueError('duplexer requires wanted RX and TX leakage inputs')
            left,right=packets['left'],packets['right']
            for port,p in packets.items():
                self.input_ports[port].validate(p)
                if not np.all(np.isfinite(p.samples)): raise ValueError('finite IQ required')
            if (left.samples.shape!=right.samples.shape or left.sample_rate!=right.sample_rate or
                    left.center_frequency!=right.center_frequency or left.timestamp!=right.timestamp):
                raise ValueError('duplexer inputs must share signal context')
            p=self._parameters
            y=left.samples*10**(-p['insertion_loss_db']/20)+right.samples*10**(-p['isolation_db']/20)
            return DataPacket(y,left.sample_rate,left.center_frequency,left.timestamp,'time',left.metadata)
        return self._measure_call('process_inputs',packets,action)


class RFBandpassComponent(RFBehavior):
    cid = 'C41'
    parameter_defaults = dict(bandwidth_hz=200000., offset_hz=0., taps=65, insertion_loss_db=0.)

    def _validate(self, p):
        positive(p['bandwidth_hz'], 'bandwidth_hz')
        integer(p['taps'], 'taps', 3)
        if p['taps'] % 2 == 0: raise ValueError('taps must be odd')
        real(p['insertion_loss_db'], 'insertion_loss_db', 0)

    def _execute(self, x):
        p, fs = self._parameters, self._active_packet.sample_rate
        if abs(p['offset_hz']) + p['bandwidth_hz']/2 >= fs/2:
            raise ValueError('RF passband must fit inside the sampled envelope Nyquist band')
        n = np.arange(p['taps']) - (p['taps']-1)/2
        h = lowpass(p['taps'], p['bandwidth_hz']/fs) * np.exp(2j*np.pi*p['offset_hz']*n/fs)
        y, history = fir(x, h, self._state.get('fir'))
        self._state['fir'] = history
        self._business = dict(group_delay_samples=(p['taps']-1)/2, simulation_only=True)
        return y * 10 ** (-p['insertion_loss_db']/20)


class LNAComponent(RFBehavior):
    cid = 'C42'
    parameter_defaults = dict(gain_db=20., noise_figure_db=2., temperature_k=290., impedance_ohm=50., noise_bandwidth_hz=100000., seed=0)

    def _validate(self, p):
        gain(p['gain_db'])
        real(p['noise_figure_db'], 'noise_figure_db', 0)
        if p['noise_figure_db'] > 100: raise ValueError('noise figure exceeds model range')
        for k in ('temperature_k', 'impedance_ohm', 'noise_bandwidth_hz'): positive(p[k], k)
        integer(p['seed'], 'seed', 0)

    def _execute(self, x):
        p = self._parameters
        if p['noise_bandwidth_hz'] > self._active_packet.sample_rate: raise ValueError('noise bandwidth exceeds sampled envelope')
        variance = 1.380649e-23 * p['temperature_k'] * p['noise_bandwidth_hz'] * p['impedance_ohm'] * np.expm1(p['noise_figure_db']*np.log(10)/10)
        draws = self._normal(len(x), 2)
        noise = (draws[:,0]+1j*draws[:,1])*np.sqrt(variance/2)
        self._business = dict(added_input_noise_v2=float(variance), simulation_only=True)
        return (x+noise)*gain(p['gain_db'])


class VGAComponent(RFBehavior):
    cid = 'C43'
    parameter_defaults = dict(gain_db=0., min_gain_db=-20., max_gain_db=60., gain_step_db=0.)

    def _validate(self, p):
        gain(p['gain_db']); gain(p['min_gain_db']); gain(p['max_gain_db'])
        if not p['min_gain_db'] <= p['gain_db'] <= p['max_gain_db']: raise ValueError('gain outside VGA bounds')
        real(p['gain_step_db'], 'gain_step_db', 0)

    def _execute(self, x):
        p = self._parameters
        g = p['gain_db']
        if p['gain_step_db']: g = np.clip(p['min_gain_db']+np.rint((g-p['min_gain_db'])/p['gain_step_db'])*p['gain_step_db'], p['min_gain_db'], p['max_gain_db'])
        self._business = dict(applied_gain_db=float(g), simulation_only=True)
        return x*gain(g)


class AnalogIQMixerComponent(RFBehavior):
    cid = 'C44'
    parameter_defaults = dict(offset_hz=0., direction='down', conversion_gain_db=0., iq_gain_error_db=0., iq_phase_error_deg=0., lo_leakage_v=0.)

    def _validate(self, p):
        if p['direction'] not in ('up','down'): raise ValueError('direction must be up or down')
        gain(p['conversion_gain_db']); gain(p['iq_gain_error_db'])
        real(p['lo_leakage_v'], 'lo_leakage_v', 0)

    def _execute(self, x):
        p, fs = self._parameters, self._active_packet.sample_rate
        if abs(p['offset_hz']) >= fs/2: raise ValueError('mixer offset exceeds Nyquist')
        phase = np.deg2rad(p['iq_phase_error_deg'])
        z = x.real + 1j*gain(p['iq_gain_error_db'])*(x.imag*np.cos(phase)+x.real*np.sin(phase))
        sign = 1 if p['direction']=='up' else -1
        y = (z+p['lo_leakage_v'])*np.exp(sign*2j*np.pi*p['offset_hz']*self._index(len(x))/fs)
        self._business = dict(simulation_only=True, equivalent_baseband=True)
        return y*gain(p['conversion_gain_db'])


class LOPhaseNoiseComponent(RFBehavior):
    cid = 'C45'
    parameter_defaults = dict(offset_hz=0., initial_error_hz=0., settling_tau_s=0.001, phase_diffusion_rad2_per_s=0., initial_phase_rad=0., seed=0)

    def _validate(self, p):
        positive(p['settling_tau_s'], 'settling_tau_s')
        real(p['phase_diffusion_rad2_per_s'], 'phase_diffusion_rad2_per_s', 0)
        integer(p['seed'], 'seed', 0)

    def _execute(self, x):
        p, fs = self._parameters, self._active_packet.sample_rate
        if abs(p['offset_hz'])+abs(p['initial_error_hz']) >= fs/2: raise ValueError('LO offsets exceed Nyquist')
        t = self._index(len(x))/fs
        phi = p['initial_phase_rad']+2*np.pi*(p['offset_hz']*t+p['initial_error_hz']*p['settling_tau_s']*(-np.expm1(-t/p['settling_tau_s'])))
        w = np.cumsum(self._normal(len(x))[:,0])*np.sqrt(p['phase_diffusion_rad2_per_s']/fs)+self._state.get('phase_noise',0.)
        self._state['phase_noise'] = float(w[-1])
        self._business = dict(residual_error_hz=float(p['initial_error_hz']*np.exp(-t[-1]/p['settling_tau_s'])), simulation_only=True)
        return x*np.exp(1j*(phi+w))


class ADCComponent(RFBehavior):
    cid = 'C46'
    parameter_defaults = dict(bits=12, full_scale_v=1., decimation=1)

    def _validate(self, p):
        integer(p['bits'], 'bits', 2); integer(p['decimation'], 'decimation')
        if p['bits'] > 24 or p['decimation'] > 64: raise ValueError('converter configuration too large')
        positive(p['full_scale_v'], 'full_scale_v')

    def _quantize(self, x):
        p = self._parameters
        step = 2*p['full_scale_v']/2**p['bits']
        limit = 2**(p['bits']-1)
        i, q = np.rint(x.real/step), np.rint(x.imag/step)
        clipped = (i < -limit)|(i > limit-1)|(q < -limit)|(q > limit-1)
        self._business = dict(lsb_v=step, clipped_samples=int(np.count_nonzero(clipped)), bits=p['bits'], simulation_only=True)
        return (np.clip(i,-limit,limit-1)+1j*np.clip(q,-limit,limit-1))*step

    def _execute(self, x):
        d = self._parameters['decimation']
        n = self._index(len(x))
        selected = np.flatnonzero(n % d == 0)
        self._state['first'] = int(selected[0]) if len(selected) else 0
        return self._quantize(x)[selected]

    def _output_context(self, packet):
        return packet.sample_rate/self._parameters['decimation'], packet.center_frequency, packet.timestamp+self._state['first']/packet.sample_rate


class DACComponent(ADCComponent):
    cid = 'C47'
    parameter_defaults = dict(bits=12, full_scale_v=1., interpolation=1)

    def _validate(self, p):
        super()._validate(dict(bits=p['bits'],full_scale_v=p['full_scale_v'],decimation=p['interpolation']))

    def _execute(self, x):
        return np.repeat(self._quantize(x), self._parameters['interpolation'])

    def _output_context(self, packet):
        return packet.sample_rate*self._parameters['interpolation'], packet.center_frequency, packet.timestamp


class PowerAmplifierComponent(RFBehavior):
    cid = 'C48'
    parameter_defaults = dict(gain_db=10., saturation_v=1., smoothness=2., am_pm_rad=0.)

    def _validate(self, p):
        gain(p['gain_db']); positive(p['saturation_v'],'saturation_v')
        if not 1 <= p['smoothness'] <= 10: raise ValueError('smoothness must be in [1,10]')

    def _execute(self, x):
        p = self._parameters
        amplitude = np.abs(x)
        log_r = np.log(np.maximum(amplitude,np.finfo(float).tiny))+p['gain_db']*np.log(10)/20-np.log(p['saturation_v'])
        out_amplitude = p['saturation_v']*np.exp(log_r-np.logaddexp(0,2*p['smoothness']*log_r)/(2*p['smoothness']))
        fraction = np.exp(-np.logaddexp(0,-log_r))
        direction = np.divide(x,amplitude,out=np.zeros_like(x),where=amplitude>0)
        self._business = dict(compressed_samples=int(np.count_nonzero(log_r>0)), simulation_only=True)
        return direction*out_amplitude*np.exp(1j*p['am_pm_rad']*fraction**2)


class ReferenceClockComponent(RFBehavior):
    cid = 'C49'
    parameter_defaults = dict(jitter_rms_s=0., reference_frequency_hz=0., phase_offset_rad=0., seed=0)

    def _validate(self, p):
        real(p['jitter_rms_s'],'jitter_rms_s',0); real(p['reference_frequency_hz'],'reference_frequency_hz',0)
        integer(p['seed'],'seed',0)

    def _execute(self, x):
        p, fs = self._parameters, self._active_packet.sample_rate
        if p['jitter_rms_s']*(p['reference_frequency_hz']+fs/2) > 0.01:
            raise ValueError('jitter too large for first-order model')
        previous = self._state.get('last_sample',x[0])
        derivative = (x-np.concatenate(([previous],x[:-1])))*fs
        self._state['last_sample'] = x[-1]
        dt = self._normal(len(x))[:,0]*p['jitter_rms_s']
        y = x+dt*(derivative+2j*np.pi*p['reference_frequency_hz']*x)
        self._business = dict(jitter_rms_s=p['jitter_rms_s'],simulation_only=True)
        return y*np.exp(1j*p['phase_offset_rad'])


RF_BEHAVIOR_COMPONENTS = (AntennaApertureComponent, RFSwitchComponent, DuplexerComponent,
    RFBandpassComponent, LNAComponent, VGAComponent, AnalogIQMixerComponent,
    LOPhaseNoiseComponent, ADCComponent, DACComponent, PowerAmplifierComponent,
    ReferenceClockComponent)

# Expose the declared keyword configuration to registry/OCS introspection.
from inspect import Signature, Parameter
for _model in RF_BEHAVIOR_COMPONENTS:
    if issubclass(_model, RFBehavior):
        _model.__signature__ = Signature([Parameter(k, Parameter.KEYWORD_ONLY, default=v)
                                         for k,v in _model.parameter_defaults.items()])
