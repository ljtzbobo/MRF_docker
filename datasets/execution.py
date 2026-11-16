"""Lazy parallel data path: record -> frames -> the existing source boundary."""
from copy import deepcopy

from .catalog import DatasetError
from .adapters import open_reader


class DatasetFrames:
    def __init__(self,catalog,task):
        from task_repr import parse_external_task
        from matching import match_task
        normalized=parse_external_task(task)
        data=task['data_input'];self.binding=deepcopy(data.get('dataset_binding'))
        if not isinstance(self.binding,dict) or self.binding.get('schema_version')!='rf_dataset_binding.v1':raise DatasetError('task has no dataset binding')
        record=catalog.record(self.binding['record_id'])
        if record['status']!='ready':raise DatasetError('record is description-only or not verified; execution adapter is unavailable')
        if record['sha256']!=self.binding['source_sha256'] or data.get('stream_id')!=record['record_id']:
            raise DatasetError('task references a different source revision')
        self.reader=open_reader(catalog,record,self.binding.get('observation_index',0))
        if data['format']!=self.reader.format or data.get('sample_rate_hz')!=self.reader.signal['sample_rate_hz']:
            raise DatasetError('task input and adapter metadata disagree')
        for key,upper in (('sample_start',None),('frame_length',1048576),('frame_count',1024)):
            value=self.binding.get(key)
            if type(value) is not int or value<(0 if key=='sample_start' else 1) or (upper and value>upper):raise DatasetError('invalid dataset window')
        self.reader.window(self.binding['sample_start'],self.binding['frame_length']*len(self))
        match=match_task(normalized)
        if match.status!='MATCHED':raise DatasetError('task can be represented but its processing chain is not implemented: '+match.status)
        boundaries=[b for b in match.execution_graph['boundaries'] if b['direction']=='source']
        if not boundaries or any(b['format']!=self.reader.format for b in boundaries):raise DatasetError('adapter needs compatible data source boundaries')
        if len({b['business_node_id'] for b in boundaries})!=1:raise DatasetError('one recording cannot supply distinct independent sources')
        for boundary in boundaries:
            node=next(n for n in match.execution_graph['nodes'] if n['id']==boundary['node_id'])
            if node.get('specification_parameters',{}).get('input_block_length',self.binding['frame_length'])!=self.binding['frame_length']:
                raise DatasetError('task processing frame length and dataset binding disagree')
            task_rate=node.get('specification_parameters',{}).get('sample_rate_mhz')
            if task_rate is not None and task_rate*1e6!=self.reader.signal['sample_rate_hz']:
                raise DatasetError('processing sample rate differs from actual dataset metadata')
        self.source_node=boundaries[0]['node_id']
        self.boundaries=boundaries

    def __len__(self):return self.binding['frame_count']

    def __iter__(self):
        for index in range(len(self)):
            start=self.binding['sample_start']+index*self.binding['frame_length']
            packet=self.reader.read(start,self.binding['frame_length']);frame={}
            for boundary in self.boundaries:frame.setdefault(boundary['node_id'],{})[boundary['port']]=packet
            yield {node:next(iter(ports.values())) if len(ports)==1 else ports for node,ports in frame.items()}


def evaluate(result):
    """Signal diagnostics only; no invented target, transcript or emitter accuracy."""
    import numpy as np
    outputs=[]
    for i,frame in enumerate(result.frame_outputs):
        for node,packet in frame.items():
            data=packet.samples
            outputs.append(dict(frame=i,node_id=node,items=packet.item_count,domain=packet.domain,
                finite=bool(np.isfinite(data).all()),mean_power=float(np.mean(np.abs(data)**2)) if data.size else None))
    return dict(kind='signal_diagnostics',outputs=outputs,ground_truth_metrics=None,
        note='No label-based accuracy claimed; decoder/model and aligned reference labels are required for scoring.')
