"""Business scenario + verified record metadata -> external_rf_task.v1 only."""
from copy import deepcopy
from hashlib import sha256
import json
import math

from .catalog import DatasetError
from .adapters import open_reader


def number(value,name,low=0,high=None,integer=False):
    try:finite=math.isfinite(value) if type(value) in (int,float) else False
    except OverflowError:finite=False
    if type(value) not in ((int,) if integer else (int,float)) or not finite or value<low or (high is not None and value>high):
        raise DatasetError('invalid experiment.'+name)
    return value


def generate(catalog,scenario_id,record_id,experiment=None):
    scenario=catalog.scenario(scenario_id);record=catalog.record(record_id)
    if record['status']!='ready' and not (record['status']=='description_ready' and scenario['builder']=='radar_business'):
        raise DatasetError('record is not ready: '+str(record.get('missing_metadata',record['status'])))
    if not record.get('sha256'):raise DatasetError('index and hash the source first: python -m datasets scan --hash')
    if record['signal']['representation']!=scenario['input_representation']:raise DatasetError('scenario input and dataset representation are incompatible')
    experiment={} if experiment is None else experiment
    if not isinstance(experiment,dict):raise DatasetError('experiment must be an object')
    allowed=set(scenario['defaults'])|{'sample_start','task_id','performance','observation_index'}
    if set(experiment)-allowed:raise DatasetError('unknown experiment parameters: '+str(sorted(set(experiment)-allowed)))
    values={**scenario['defaults'],**deepcopy(experiment)}
    start=number(values.get('sample_start',record['signal'].get('sample_offset',0)),'sample_start',integer=True)
    length=number(values['frame_length'],'frame_length',1,1048576,True)
    count=number(values['frame_count'],'frame_count',1,1024,True)
    observation=number(values.get('observation_index',0),'observation_index',integer=True)
    if record['adapter']!='iridium_mat' and observation:raise DatasetError('observation_index only applies to burst collections')
    reader=open_reader(catalog,record,observation);reader.window(start,length*count)
    rate=record['signal'].get('sample_rate_hz') or record['signal'].get('pulse_repetition_hz')
    frequency=reader.window(start,length*count).get('core:frequency') if record['adapter']=='sigmf' else record['signal']['center_frequency_hz']
    band=scenario.get('frequency_range_hz')
    if band and (frequency is None or not band[0]<=frequency<=band[1]):raise DatasetError('record frequency is outside the selected scenario')
    arrival=number(values['arrival_ms'],'arrival_ms')
    priority=number(values['priority'],'priority',0,10,True)
    request=deepcopy(scenario['request'])
    if scenario['builder']=='ddc_fir_psd':
        decimation=number(values['decimation'],'decimation',1,1024,True)
        fft=number(values['fft_length'],'fft_length',2,length,True)
        number(values['nco_frequency_hz'],'nco_frequency_hz',-rate/2,rate/2)
        number(values['filter_cutoff'],'filter_cutoff',1e-12,.499999)
        number(values['overlap'],'overlap',0,.999999)
        if length%decimation or length//decimation<fft:raise DatasetError('frame must divide by decimation and contain at least one output FFT window')
        request['processing']=dict(decimation=decimation,nco_frequency_mhz=values['nco_frequency_hz']/1e6,
            input_sample_rate_mhz=rate/1e6,baseband_sample_rate_mhz=rate/decimation/1e6,
            input_block_length=length,baseband_block_length=length//decimation,
            filter_cutoff=values['filter_cutoff'],fft_length=fft,overlap=values['overlap'])
    elif scenario['builder'] not in ('business_only','radar_business'):raise DatasetError('unsupported scenario builder')
    binding=dict(schema_version='rf_dataset_binding.v1',record_id=record_id,source_sha256=record['sha256'],
        sample_start=start,frame_length=length,frame_count=count,observation_index=observation,
        scenario_id=scenario_id,scenario_sha256=sha256(json.dumps(scenario,sort_keys=True).encode()).hexdigest())
    identity=sha256(json.dumps(dict(binding=binding,experiment=values),sort_keys=True).encode()).hexdigest()[:16]
    task=dict(schema_version='external_rf_task.v1',task_id=values.get('task_id','DATA-'+identity),
        family=scenario['family'],task_type=scenario['task_type'],direction=scenario['direction'],
        arrival_ms=arrival,priority=priority,duration_ms=length*count/rate*1000,
        request=request,performance=deepcopy(values.get('performance',{})),
        data_input=dict(kind='complex_iq_stream' if reader.format=='iq/complex' else 'application_audio_stream',
            stream_id=record_id,binding_status='dataset_record_bound',format=reader.format,
            sample_rate_hz=record['signal']['sample_rate_hz'],center_frequency_hz=frequency,channels=record['signal']['channels'],
            dataset_binding=binding),expected_output=scenario['expected_output'],scenario=scenario['title'])
    if values.get('deadline_after_ms') is not None:
        budget=number(values['deadline_after_ms'],'deadline_after_ms',1e-9)
        task['deadline_ms']=arrival+budget
    if scenario['builder']=='radar_business':
        binding['index_unit']='slow_time_pulse'
        task['data_input'].update(kind='radar_echo_matrix',pulse_repetition_hz=rate,
            range_bin_spacing_m=record['signal']['range_bin_spacing_m'],range_bins=record['signal']['range_bins'])
    # This validates the existing public input contract; no internal task template is read.
    from task_repr import parse_external_task
    parse_external_task(task)
    return dict(task=task,binding=binding,provenance=record['provenance'],limitations=scenario.get('limitations',[]),
        scope='external description only; adapters read samples at execution time')


def compatibility(task):
    from matching import match_task
    match=match_task(task)
    return dict(match_status=match.status,executable_components=match.status=='MATCHED',
        issues=[i.to_dict() for i in match.unresolved])
