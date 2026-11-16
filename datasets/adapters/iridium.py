"""Explicit IridiumRF-MR layout: h5py [I/Q, sample, observation]."""
import numpy as np
from components.runtime import DataPacket
from ..catalog import DatasetError


class IridiumReader:
    format='iq/complex'

    def __init__(self,catalog,record,observation_index=0):
        self.catalog=catalog;self.path=catalog.source(record);self.record=record;self.signal=record['signal']
        if type(observation_index) is not int or not 0<=observation_index<self.signal['observation_count']:
            raise DatasetError('observation_index is outside the burst collection')
        self.observation_index=observation_index

    def window(self,start,count):
        if type(start) is not int or type(count) is not int or start<0 or count<1 or start+count>self.signal['sample_count']:
            raise DatasetError('window exceeds this individual Iridium burst; observations cannot be concatenated')
        return {}

    def read(self,start,count):
        self.catalog.source(self.record)
        self.window(start,count)
        if count*16>16*2**20:raise DatasetError('frame exceeds packet limit')
        try:import h5py
        except ImportError as exc:raise DatasetError('install datasets/requirements.txt for Iridium MAT execution') from exc
        with h5py.File(self.path,'r') as source:
            array=source[self.record['variable']]
            if list(array.shape)!=[2,self.signal['sample_count'],self.signal['observation_count']]:
                raise DatasetError('Iridium array dimensions changed; rescan required')
            chunk=array[:,start:start+count,self.observation_index]
        self.catalog.source(self.record)
        samples=chunk[0].astype(np.float64)+1j*chunk[1].astype(np.float64)
        if not np.isfinite(samples).all():raise DatasetError('non-finite burst samples')
        return DataPacket(samples,self.signal['sample_rate_hz'],self.signal['center_frequency_hz'],
            start/self.signal['sample_rate_hz'],'time',dict(dataset_record_id=self.record['record_id'],
                observation_index=self.observation_index,field=self.record['variable'],sample_start=start,
                time_reference='within_burst_relative',amplitude_units='source_preprocessed_normalized',
                source_kind='preprocessed_burst_not_continuous_rf',source_sha256=self.record['sha256']))

    def reference(self):
        import h5py
        with h5py.File(self.path,'r') as source:
            return dict(observation_index=self.observation_index,satellite_label=int(source['satlabel'][self.observation_index,0]),
                snr_db=float(source['snr'][self.observation_index,0]),doppler_hz=float(source['Doppler'][self.observation_index,0]))
