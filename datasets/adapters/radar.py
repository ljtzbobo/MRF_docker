"""Radar matrix reference windows, intentionally not raw-IQ packets."""
from ..catalog import DatasetError


class RadarReference:
    format='radar/range_pulse_matrix'

    def __init__(self,catalog,record):
        catalog.source(record);self.record=record;self.signal=record['signal']

    def window(self,start,count):
        if type(start) is not int or type(count) is not int or start<0 or count<1 or start+count>self.signal['pulse_count']:
            raise DatasetError('pulse window exceeds radar matrix')
        return {}

    def read(self,start,count):
        raise DatasetError('radar matrix is a description-only source; pulse/range/gate adapter and a matching processing chain are required')
