"""Explicit offline indexing: headers and archive members, never guessed RF rates."""
from collections import Counter
from hashlib import sha256,sha512
import json
from pathlib import Path
import tarfile

from .catalog import DatasetCatalog,DatasetError
from .adapters.sigmf import describe
from .adapters.wav import safe_member,wave_info


def digest(path):
    h=sha256();s=sha512()
    with path.open('rb') as source:
        for block in iter(lambda:source.read(8*2**20),b''):h.update(block);s.update(block)
    return h.hexdigest(),s.hexdigest()


def mat_variables(path):
    with path.open('rb') as source:header=source.read(128)
    if b'MATLAB 7.3' in header:
        try:import h5py
        except ImportError:return dict(container='matlab_v7.3_hdf5',variables=[],inspection_error='Install datasets/requirements.txt to inspect HDF5 metadata')
        variables=[]
        with h5py.File(path,'r') as file:
            for name,obj in file.items():
                if isinstance(obj,h5py.Dataset):variables.append(dict(name=name,shape=list(obj.shape),dtype=str(obj.dtype)))
        return dict(container='matlab_v7.3_hdf5',variables=variables)
    try:
        from scipy.io import whosmat
        variables=[dict(name=name,shape=list(shape),dtype=kind) for name,shape,kind in whosmat(path)]
        return dict(container='matlab_v5',variables=variables)
    except ImportError:return dict(container='matlab_v5',variables=[],inspection_error='Install datasets/requirements.txt to inspect MAT headers')


def scan(root,*,hash_files=False):
    root=Path(root).resolve();previous=DatasetCatalog(root).read()
    previous_files={f['path']:f for f in previous['files']}
    sources_path=root/'sources.json'
    sources=json.loads(sources_path.read_text(encoding='utf-8')) if sources_path.exists() else {}
    files=[];records=[];warnings=[];hashes={}
    def file_info(path):
        relative=path.relative_to(root).as_posix()
        if relative in hashes:return hashes[relative]
        stat=path.stat();info=dict(path=relative,bytes=stat.st_size,mtime_ns=stat.st_mtime_ns,sha256=None,sha512=None,
            provenance=sources.get(relative,dict(source_url=None,license=None,data_origin='not_verified')))
        old=previous_files.get(relative,{})
        if hash_files:info['sha256'],info['sha512']=digest(path)
        elif all(old.get(k)==info[k] for k in ('bytes','mtime_ns')):
            info['sha256'],info['sha512']=old.get('sha256'),old.get('sha512')
        hashes[relative]=info;files.append(info);return info
    def record(path,adapter,signal,**extra):
        info=file_info(path);suffix=extra.get('archive_member',extra.get('variable',''))
        identifier='record-'+sha256((info['path']+'!'+suffix).encode()).hexdigest()[:16]
        entry=dict(record_id=identifier,title=suffix or info['path'],path=info['path'],file_bytes=info['bytes'],
            file_mtime_ns=info['mtime_ns'],sha256=info['sha256'],adapter=adapter,signal=signal,provenance=info['provenance'],
            status='ready' if adapter in ('sigmf','wav','tar_wav','iridium_mat') else 'needs_metadata',labels=[],**extra)
        records.append(entry);return entry
    # Limit traversal to known data formats; Python sources/catalogs are not recordings.
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.is_symlink() or any(p.startswith('.') for p in path.relative_to(root).parts):continue
        suffix=path.suffix.lower()
        try:
            if suffix=='.sigmf-meta':
                data=path.with_suffix('.sigmf-data');file_info(path)
                signal=describe(path,data);entry=record(data,'sigmf',signal,metadata_path=path.relative_to(root).as_posix())
                if signal['declared_sha512'] and entry['sha256']:
                    verified=file_info(data)['sha512']==signal['declared_sha512']
                    entry['checksum_verified']=verified
                    if not verified:entry['status']='integrity_error';warnings.append('SigMF checksum mismatch: '+entry['path'])
            elif suffix=='.wav':
                with path.open('rb') as stream:record(path,'wav',wave_info(stream))
            elif suffix=='.tgz' or path.name.endswith('.tar.gz') or suffix=='.tar':
                file_info(path)
                with tarfile.open(path) as archive:
                    members=[];audio=[]
                    for member in archive:
                        if not member.isfile():continue
                        safe_member(member.name);members.append(member.name)
                        if member.name.lower().endswith('.wav'):
                            if member.size>128*2**20:raise DatasetError('audio member is too large')
                            with archive.extractfile(member) as stream:signal=wave_info(stream)
                            audio.append(record(path,'tar_wav',signal,archive_member=member.name,member_bytes=member.size))
                    names=set(members)
                    for entry in audio:
                        base=entry['archive_member'][:-4]
                        entry['labels']=[dict(archive_member=base+ext,meaning='source annotation; inspect schema before scoring')
                            for ext in ('.xml','.info') if base+ext in names]
            elif suffix=='.mat':
                info=mat_variables(path)
                provenance=file_info(path)['provenance']
                if path.name in ('label_mapping.mat','data1_gate.mat'):
                    file_info(path)['mat']=info
                    file_info(path)['role']='label_mapping' if path.name=='label_mapping.mat' else 'range_gate_auxiliary'
                    continue
                if provenance.get('dataset_id')=='dim-aircraft-radar' and path.name=='data1.mat':
                    variable=next((v for v in info['variables'] if v['name']=='radar_pulse_squence'),None)
                    if variable is not None and len(variable['shape'])==2:
                        entry=record(path,'radar_reference',dict(representation='radar_range_pulse_matrix',
                            sample_rate_hz=None,center_frequency_hz=35000000000,sample_count=None,sample_offset=0,
                            channels=1,pulse_repetition_hz=32000,range_bin_spacing_m=1.875,
                            range_bins=variable['shape'][0],pulse_count=variable['shape'][1],
                            parameter_source='https://doi.org/10.11922/csdata.2019.0075.zh'),mat=info,
                            variable='radar_pulse_squence')
                        entry['status']='description_ready'
                        entry['missing_metadata']=['range_pulse_gate_processing_adapter','matching_radar_component_chain']
                        entry['labels']=[dict(path=path.with_name('data1_value.txt').relative_to(root).as_posix(),
                            meaning='time_ms, target_count, target_id, radial_velocity_m_s, range_m; source labels every ~50 ms')]
                        continue
                if provenance.get('dataset_id')=='iridiumrf-mr' and path.name=='data_pluto1.mat':
                    variables={v['name']:v for v in info['variables']}
                    if not {'QPSK','BPSK','pilot','satlabel','snr','Doppler'}<=variables.keys():
                        raise DatasetError('Iridium variable layout does not match the documented source')
                    observations=variables['QPSK']['shape'][2]
                    if variables['satlabel']['shape']!=[observations,1]:raise DatasetError('Iridium signal/label count mismatch')
                    for name in ('QPSK','BPSK','pilot'):
                        shape=variables[name]['shape']
                        if len(shape)!=3 or shape[0]!=2 or shape[2]!=observations:raise DatasetError('unsupported Iridium I/Q axes')
                        entry=record(path,'iridium_mat',dict(representation='complex_iq',sample_rate_hz=960000,
                            center_frequency_hz=1626250000,sample_count=shape[1],sample_offset=0,channels=1,
                            observation_count=observations,parameter_source=provenance['description_url'],
                            processing='source preprocessed bursts; acquisition sample rate retained; no resampling performed'),
                            variable=name,mat=info)
                        entry['title']='IridiumRF-MR PLUTO1 / '+name+' / '+str(observations)+' bursts'
                        entry['labels']=[dict(variable='satlabel',meaning='0..65 satellite identity; never fed to components as predictions')]
                    continue
                entry=record(path,'unconfigured_mat',dict(representation='unknown',sample_rate_hz=None,center_frequency_hz=None,
                    sample_count=None,sample_offset=0,channels=None),mat=info)
                entry['missing_metadata']=['signal_variable_and_axes','sample_rate_hz','center_frequency_hz','label_alignment']
            elif suffix in ('.001','.7z') or path.name.endswith('_value.txt'):
                file_info(path)
        except (OSError,ValueError,KeyError,tarfile.TarError,EOFError) as exc:
            warnings.append(path.relative_to(root).as_posix()+': '+str(exc))
    data=dict(schema_version='rf_dataset_catalog.v1',files=files,records=records,warnings=warnings,
        summary=dict(files=len(files),records=len(records),by_adapter=dict(Counter(r['adapter'] for r in records)),
            ready=sum(r['status']=='ready' for r in records),description_ready=sum(r['status']=='description_ready' for r in records)),
        notes=['Original paths retained; no extraction, renaming or signal resampling performed.',
               'Dataset names/labels do not establish RF parameters, platform provenance or an available decoder.'])
    temporary=root/'catalog.json.tmp';temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    temporary.replace(root/'catalog.json')
    return data
