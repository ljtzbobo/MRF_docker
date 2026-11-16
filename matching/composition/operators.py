"""Local component effects/parameter bindings. No task-to-chain recipes."""
import inspect
import json
from pathlib import Path

from components.implementations.runtime_registry import RUNTIME_COMPONENT_REGISTRY
from components.specs import load_component_specs
from .models import Operator


def load_operators(capability_map):
    interfaces=load_component_specs()
    metadata=json.loads((Path(__file__).parent/'operators.json').read_text(encoding='utf-8'))
    operators=[]
    for mapping in capability_map.mappings:
        if mapping.cid not in interfaces or mapping.cid not in RUNTIME_COMPONENT_REGISTRY:continue
        spec=interfaces[mapping.cid]
        for capability in mapping.capabilities:
            info=metadata.get(mapping.cid,{})
            mode=('ifft' if capability=='ifft' else info.get('mode'))
            inputs={}
            outputs=()
            for name,port in spec.ports.items():
                formats=port.protocols if port.protocols_by_mode is None else port.protocols_by_mode.get(mode,())
                if port.direction=='input':inputs[name]=tuple(formats)
                else:outputs=tuple(formats)
            if not inputs or not outputs:continue
            parameters=dict(info.get('parameters',{}))
            if mode:parameters['mode']={'constant':mode}
            kind='ifft' if capability=='ifft' else info.get('kind','declared')
            operators.append(Operator(mapping.cid+':'+capability,mapping.cid,capability,inputs,outputs,
                parameters,kind,info.get('automatic',False) and capability==mapping.capabilities[0],info.get('cost',1)))
    return tuple(operators)


def parameter_values(operator,processing,local):
    constructor=RUNTIME_COMPONENT_REGISTRY[operator.cid]
    accepted=getattr(constructor,'parameter_defaults',inspect.signature(constructor).parameters)
    values={key:value for key,value in processing.items() if key in accepted}
    for target,source in operator.parameters.items():
        if isinstance(source,dict):
            if 'constant' in source:values[target]=source['constant']
            elif source['field'] in processing:values[target]=processing[source['field']]
        elif source in processing:values[target]=processing[source]
    values.update(local)
    # Preserve the static matcher's existing encoded-array input convention.
    for name in ('coefficients','denominator','band_edges','preamble','channel_map','prototype_filter',
                 'subcarrier_allocation','pilot_pattern','pilot_values','guard_carriers','generators',
                 'reference_waveform','range_gate','gains_db','phases_deg'):
        if name in values and isinstance(values[name],list):values[name]=json.dumps(values[name])
    return values
