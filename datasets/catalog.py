"""Portable record references. No imports from the task representation module."""
from copy import deepcopy
import json
from pathlib import Path
import re


class DatasetError(ValueError):
    pass


def inside(root, reference):
    if not isinstance(reference,str) or not reference or '\\' in reference:
        raise DatasetError('data paths must be nonempty relative POSIX paths')
    relative=Path(reference)
    if relative.is_absolute() or ':' in reference or '..' in relative.parts:
        raise DatasetError('data path must stay within the dataset root')
    target=(Path(root)/relative).resolve()
    if not target.is_relative_to(Path(root).resolve()):
        raise DatasetError('data path escapes the dataset root')
    return target


class DatasetCatalog:
    def __init__(self,root):
        self.root=Path(root).resolve()

    def read(self):
        path=self.root/'catalog.json'
        if not path.exists():return dict(schema_version='rf_dataset_catalog.v1',files=[],records=[],warnings=['Run python -m datasets scan first'])
        try:data=json.loads(path.read_text(encoding='utf-8'))
        except (OSError,ValueError) as exc:raise DatasetError('invalid dataset catalog: '+str(exc)) from exc
        if data.get('schema_version')!='rf_dataset_catalog.v1':raise DatasetError('unsupported catalog version')
        records=data.get('records',[])
        if len({r['record_id'] for r in records})!=len(records):raise DatasetError('duplicate record IDs')
        return data

    def record(self,record_id):
        for record in self.read()['records']:
            if record['record_id']==record_id:return deepcopy(record)
        raise DatasetError('unknown record_id')

    def source(self,record):
        source=inside(self.root,record['path'])
        if not source.is_file():raise DatasetError('dataset source is not present on this machine: '+record['path'])
        if source.stat().st_size!=record['file_bytes']:raise DatasetError('dataset file size changed; rescan and verify it')
        if 'file_mtime_ns' in record and source.stat().st_mtime_ns!=record['file_mtime_ns']:
            raise DatasetError('source modification time changed; rescan and verify it')
        return source

    def scenarios(self):
        path=self.root/'scenarios'
        return [json.loads(p.read_text(encoding='utf-8')) for p in sorted(path.glob('*.json'))]

    def scenario(self,scenario_id):
        if not isinstance(scenario_id,str) or not re.fullmatch('[a-z0-9_-]+',scenario_id):
            raise DatasetError('invalid scenario_id')
        path=self.root/'scenarios'/(scenario_id+'.json')
        if not path.is_file():raise DatasetError('unknown scenario_id')
        result=json.loads(path.read_text(encoding='utf-8'))
        if result.get('schema_version')!='rf_dataset_scenario.v1' or result['scenario_id']!=scenario_id:
            raise DatasetError('invalid scenario file')
        return result
