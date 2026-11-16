"""Adapters emit one bounded DataPacket at a time; never embed samples in tasks."""
from .sigmf import SigMFReader
from .wav import WaveReader
from ..catalog import DatasetError


def open_reader(catalog,record,observation_index=0):
    if record.get('adapter')=='sigmf':return SigMFReader(catalog,record)
    if record.get('adapter') in ('wav','tar_wav'):return WaveReader(catalog,record)
    if record.get('adapter')=='iridium_mat':
        from .iridium import IridiumReader
        return IridiumReader(catalog,record,observation_index)
    if record.get('adapter')=='radar_reference':
        from .radar import RadarReference
        return RadarReference(catalog,record)
    raise DatasetError('record needs an explicit adapter configuration and verified signal metadata')
