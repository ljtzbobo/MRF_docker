"""PCM WAV files, including tar members, without extraction or whole-file loads."""
from contextlib import contextmanager
from pathlib import PurePosixPath
import tarfile
import wave

import numpy as np
from components.runtime import DataPacket
from ..catalog import DatasetError


def safe_member(name):
    p=PurePosixPath(name)
    if p.is_absolute() or '..' in p.parts or '\\' in name or ':' in name:raise DatasetError('unsafe archive member')
    return name


def wave_info(stream):
    with wave.open(stream,'rb') as reader:
        if reader.getcomptype()!='NONE' or reader.getsampwidth() not in (1,2,3,4):raise DatasetError('only uncompressed integer PCM WAV is supported')
        return dict(representation='audio_pcm',sample_rate_hz=reader.getframerate(),channels=reader.getnchannels(),
            sample_width_bytes=reader.getsampwidth(),sample_count=reader.getnframes(),sample_offset=0,
            center_frequency_hz=None)


class WaveReader:
    format='audio/pcm'

    def __init__(self,catalog,record):
        self.catalog=catalog;self.record=record;self.path=catalog.source(record);self.signal=record['signal']

    @contextmanager
    def open(self):
        if self.record['adapter']=='tar_wav':
            with tarfile.open(self.path) as archive:
                name=safe_member(self.record['archive_member'])
                member=next((entry for entry in archive if entry.name==name),None)
                if member is None:raise DatasetError('audio member is missing')
                if not member.isfile() or member.size>128*2**20:raise DatasetError('archive member is not a bounded regular audio file')
                with archive.extractfile(member) as source:
                    yield source
        else:
            with self.path.open('rb') as source:yield source

    def window(self,start,count):
        if type(start) is not int or type(count) is not int or start<0 or count<1 or start+count>self.signal['sample_count']:
            raise DatasetError('audio window exceeds the recording')
        if self.signal['channels']!=1:raise DatasetError('select an explicit mono channel before task generation; multi-channel is deferred')

    def read(self,start,count):
        self.catalog.source(self.record)
        self.window(start,count)
        if count*8>16*2**20:raise DatasetError('frame exceeds the 16 MiB packet limit')
        with self.open() as source, wave.open(source,'rb') as reader:
            if (reader.getframerate()!=self.signal['sample_rate_hz'] or reader.getnframes()!=self.signal['sample_count'] or reader.getnchannels()!=1):
                raise DatasetError('WAV metadata differs from catalog; rescan required')
            width=reader.getsampwidth();reader.setpos(start);raw=reader.readframes(count)
        self.catalog.source(self.record)
        if len(raw)!=width*count:raise DatasetError('truncated audio frame')
        if width==1:data=(np.frombuffer(raw,np.uint8).astype(float)-128)/128
        elif width==3:
            b=np.frombuffer(raw,np.uint8).reshape(-1,3).astype(np.int32)
            data=b[:,0]|b[:,1]<<8|b[:,2]<<16
            data=((data^0x800000)-0x800000).astype(float)/2**23
        else:data=np.frombuffer(raw,np.dtype('<i'+str(width))).astype(float)/2**(width*8-1)
        return DataPacket(data,self.signal['sample_rate_hz'],0,start/self.signal['sample_rate_hz'],'time',
            dict(dataset_record_id=self.record['record_id'],sample_start=start,amplitude_units='relative_full_scale',
                 time_reference='record_relative',source_kind='decoded_audio_not_rf_iq'))
