"""Single-channel, conforming complex SigMF datasets; absolute sample indices."""
from datetime import datetime
import json
import math
import re

import numpy as np

from components.runtime import DataPacket
from ..catalog import DatasetError,inside


def datatype(value):
    match=re.fullmatch(r'c([if])(8|16|32|64)(?:_(le|be))?',str(value))
    if not match:raise DatasetError('unsupported complex SigMF datatype: '+str(value))
    kind,bits,order=match.groups();bits=int(bits)
    if (kind=='f' and bits not in (32,64)) or (bits>8 and order is None):
        raise DatasetError('SigMF datatype requires a supported width and explicit endianness')
    return np.dtype(('>' if order=='be' else '<')+kind+str(bits//8))


def utc_timestamp(stamp):
    match=re.fullmatch(r'(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?Z',stamp)
    if not match:raise DatasetError('capture datetime must be UTC RFC3339 with a Z suffix')
    # Python 3.10 rejects nanosecond-length fractions; retain their numeric value.
    return datetime.fromisoformat(match[1]+'+00:00').timestamp()+float('0.'+(match[2] or '0'))


def describe(meta_path,data_path):
    raw=json.loads(meta_path.read_text(encoding='utf-8'))
    glob=raw['global'];captures=raw.get('captures',[])
    if glob.get('core:num_channels',1)!=1:raise DatasetError('multi-channel SigMF is deferred')
    if glob.get('core:dataset') or glob.get('core:trailing_bytes',0) or glob.get('core:metadata_only'):
        raise DatasetError('non-conforming or metadata-only SigMF is not supported')
    if any(not e.get('optional',False) for e in glob.get('core:extensions',[])):
        raise DatasetError('required SigMF extensions are not supported')
    if any(c.get('core:header_bytes',0) for c in captures):raise DatasetError('capture headers require a non-conforming adapter')
    dtype=datatype(glob['core:datatype']);size=data_path.stat().st_size
    if size%(2*dtype.itemsize):raise DatasetError('IQ file has an incomplete complex sample')
    rate=glob.get('core:sample_rate')
    if type(rate) not in (int,float) or not math.isfinite(rate) or rate<=0:raise DatasetError('missing/invalid SigMF sample rate')
    offset=glob.get('core:offset',0)
    if type(offset) is not int or offset<0:raise DatasetError('invalid SigMF sample offset')
    starts=[c.get('core:sample_start') for c in captures]
    if not starts or any(type(x) is not int for x in starts) or starts!=sorted(set(starts)) or starts[0]!=offset:
        raise DatasetError('captures must begin at core:offset and have increasing sample_start values')
    count=size//(2*dtype.itemsize)
    if any(x<offset or x>=offset+count for x in starts):raise DatasetError('capture lies outside the signal file')
    for c in captures:
        f=c.get('core:frequency')
        if type(f) not in (int,float) or not math.isfinite(f) or f<0:raise DatasetError('each capture needs its own verified center frequency')
    return dict(datatype=glob['core:datatype'],sample_rate_hz=rate,center_frequency_hz=captures[0]['core:frequency'],
        sample_count=count,sample_offset=offset,channels=1,representation='complex_iq',captures=captures,
        annotations=raw.get('annotations',[]),declared_sha512=glob.get('core:sha512'),license=glob.get('core:license'))


class SigMFReader:
    format='iq/complex'

    def __init__(self,catalog,record):
        self.catalog=catalog;self.record=record;self.path=catalog.source(record)
        meta=inside(catalog.root,record['metadata_path'])
        self.signal=describe(meta,self.path)
        if any(self.signal.get(k)!=record['signal'].get(k) for k in
               ('datatype','sample_rate_hz','sample_count','sample_offset','captures')):
            raise DatasetError('SigMF metadata changed; rescan before generating a task')

    def window(self,start,count):
        if type(start) is not int or type(count) is not int or count<1:raise DatasetError('sample window must use integer indices and positive length')
        offset=self.signal['sample_offset'];end=start+count
        if start<offset or end>offset+self.signal['sample_count']:raise DatasetError('sample window exceeds the recording')
        captures=self.signal['captures']
        capture=next((c for i,c in enumerate(captures) if c['core:sample_start']<=start and
            (i+1==len(captures) or end<=captures[i+1]['core:sample_start'])),None)
        if capture is None:raise DatasetError('window crosses a capture boundary; split the experiment at that boundary')
        return capture

    def read(self,start,count):
        self.catalog.source(self.record)
        capture=self.window(start,count);dtype=datatype(self.signal['datatype'])
        if count*16>16*2**20:raise DatasetError('frame exceeds the 16 MiB packet limit')
        with self.path.open('rb') as stream:
            stream.seek((start-self.signal['sample_offset'])*2*dtype.itemsize)
            pair=np.fromfile(stream,dtype=dtype,count=count*2).reshape(-1,2)
        self.catalog.source(self.record)
        if len(pair)!=count:raise DatasetError('signal file was truncated during execution')
        samples=pair[:,0].astype(np.float64)+1j*pair[:,1].astype(np.float64)
        # Integer ADC counts become relative full-scale amplitudes, not calibrated volts.
        if dtype.kind=='i':samples/=2**(dtype.itemsize*8-1)
        if not np.isfinite(samples).all():raise DatasetError('non-finite IQ samples')
        elapsed=(start-capture['core:sample_start'])/self.signal['sample_rate_hz']
        stamp=capture.get('core:datetime')
        timestamp=utc_timestamp(stamp)+elapsed if stamp else (start-self.signal['sample_offset'])/self.signal['sample_rate_hz']
        return DataPacket(samples,self.signal['sample_rate_hz'],capture['core:frequency'],timestamp,'time',
            dict(dataset_record_id=self.record['record_id'],sample_start=start,sample_count=count,
                 amplitude_units='relative_full_scale' if dtype.kind=='i' else 'source_float_units',
                 time_reference='capture_utc' if stamp else 'record_relative',source_sha256=self.record.get('sha256')))
