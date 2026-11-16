"""Partial-DAG assembly. Operators are joined by contracts, not a chain table."""
from dataclasses import replace
from itertools import product
import json
import math

from components.specs import load_component_specs,load_protocol_specs
from task_repr.template.refinement import SpecificationNode,SpecificationEdge,SpecificationGraph,SpecificationTask,SpecificationSemantics
from matching.candidate_builder import validate_candidate
from matching.contracts import validate_connection
from matching.rules import MatchRuleError
from .models import Signal,Token,Step,State
from .operators import parameter_values


class CompositionProblem:
    def __init__(self,normalized,functions,operators,capability_map):
        self.normalized=normalized;self.functions={f.id:f for f in functions}
        self.operators=tuple(operators);self.capability_map=capability_map
        if len(self.functions)!=len(functions):raise ValueError('duplicate functional node ID')
        for operator in operators:
            if type(operator.cost) not in (int,float) or not math.isfinite(operator.cost) or operator.cost<0:
                raise ValueError('operator costs must be finite and nonnegative')
        self.processing=normalized['S']['business_parameters'].get('processing',{})
        self.interfaces=load_component_specs();self.protocols=load_protocol_specs()
        self.statistics=dict(candidate_checks=0,candidate_cache_hits=0,compatibility_checks=0,
            compatibility_cache_hits=0,forward_checks=0)
        self.failures={};self.candidate_cache={};self.edge_cache={}
        for function in functions:
            for key in ('sample_rate_hz','output_sample_rate_hz','input_block_length','output_block_length'):
                if key not in function.parameters:continue
                value=function.parameters[key]
                valid=type(value) is int if 'length' in key else type(value) in (int,float)
                if not valid or not math.isfinite(value) or value<=0:raise ValueError(key+' must be positive and finite')
            if set(function.port_dependencies.values())-set(function.predecessors):
                raise ValueError('named input dependency must reference a direct functional predecessor')
        data=normalized['S']['data_input']
        rate=data.get('sample_rate_hz',self.processing.get('input_sample_rate_mhz'))
        if 'sample_rate_hz' not in data and rate is not None:rate*=1e6
        length=self.processing.get('input_block_length',data.get('dataset_binding',{}).get('frame_length'))
        for value,name,integer in [(rate,'input sample rate',False),(length,'input block length',True)]:
            if value is not None and (type(value) not in ((int,) if integer else (int,float)) or not math.isfinite(value) or value<=0):
                raise ValueError(name+' must be positive and finite')
        fmt=data.get('format')
        if not isinstance(fmt,str):raise ValueError('data_input.format is needed for component assembly')
        self.initial=State((Token('external:input',Signal(fmt,rate,length)),))
        s=normalized['S']
        self.task=SpecificationTask(normalized['task_id'],SpecificationSemantics(s['family'],s['task_type'],s['direction'],{}),
            normalized['Q'],normalized['R'],SpecificationGraph((),(),(),'demand_composition','1','component_dag',{},(),(),()),normalized['X'])
        self.domains={f.id:tuple(o for o in self.operators if o.capability==f.capability) for f in functions}
        self.ancestors={}
        def ancestors(identifier,path=()):
            if identifier in path:raise ValueError('functional DAG contains a cycle')
            if identifier not in self.functions:raise ValueError('functional dependency references unknown node '+identifier)
            return frozenset().union(*(ancestors(p,path+(identifier,))|{p} for p in self.functions[identifier].predecessors))
        for f in functions:self.ancestors[f.id]=ancestors(f.id)

    def fail(self,code,message,function=None):
        key=(code,message,function)
        if len(self.failures)<64:self.failures[key]=dict(code=code,detail=message,node_id=function)

    def lower_bound(self,state):
        pending=[f for f in self.functions if f not in dict(state.assignments)]
        # max, rather than sum: one producer can feed several downstream demands.
        remaining=max((min((o.cost for o in self.domains[f]),default=math.inf) for f in pending),default=0)
        return state.cost+remaining

    def complete(self,state):return len(state.assignments)==len(self.functions)

    def _parameters(self,operator,function,signals):
        local={} if function is None else dict(function.parameters)
        metadata={k:local.pop(k) for k in tuple(local) if k in ('sample_rate_hz','input_block_length','output_block_length','output_sample_rate_hz')}
        values=parameter_values(operator,self.processing,local)
        signal=signals[0]
        if signal.rate_hz is not None:values['sample_rate_mhz']=signal.rate_hz/1e6
        if signal.length is not None:values['input_block_length']=signal.length
        values.update({k:v for k,v in metadata.items() if k in ('input_block_length','output_block_length')})
        if 'sample_rate_hz' in metadata:
            if signal.rate_hz is None or not math.isclose(signal.rate_hz,metadata['sample_rate_hz']):return None
        if 'input_block_length' in metadata and signal.length!=metadata['input_block_length']:return None
        if operator.kind=='ddc':
            if 'decimation' not in values or 'nco_frequency_mhz' not in values and 'nco_frequency' not in values:
                self.fail('NEEDS_PARAMETERS','DDC needs decimation and frequency offset',function.id if function else None);return None
            d=values['decimation']
            if type(d) is not int or d<1:return None
            output=Signal('iq/baseband',None if signal.rate_hz is None else signal.rate_hz/d,
                None if signal.length is None else (signal.length+d-1)//d)
        elif operator.kind=='psd':
            if 'fft_length' not in values or 'overlap' not in values:
                self.fail('NEEDS_PARAMETERS','processing.fft_length and processing.overlap are required',function.id if function else None);return None
            output=Signal('spectrum/psd',signal.rate_hz,values['fft_length'])
        elif operator.kind in ('fft','ifft'):
            output=Signal('frequency/complex' if operator.kind=='fft' else 'iq/complex',signal.rate_hz,values.get('fft_size'))
        elif operator.kind=='preserve':output=signal
        elif operator.kind in ('adc','dac'):
            factor=values.get('decimation' if operator.kind=='adc' else 'interpolation',1)
            if type(factor) is not int or factor<1:return None
            rate=None if signal.rate_hz is None else signal.rate_hz/factor if operator.kind=='adc' else signal.rate_hz*factor
            length=None if signal.length is None else (signal.length+factor-1)//factor if operator.kind=='adc' else signal.length*factor
            output=Signal(signal.format,rate,length)
        else:
            # No inferred resampling/shape conversion for undocumented operators.
            fmt=function.output_format if function and function.output_format else signal.format if signal.format in operator.outputs else operator.outputs[0] if len(operator.outputs)==1 else None
            if fmt is None:
                self.fail('NEEDS_PARAMETERS','ambiguous output contract; specify output_format',function.id if function else None);return None
            output=Signal(fmt,metadata.get('output_sample_rate_hz'),metadata.get('output_block_length'))
        if function and function.output_format and output.format!=function.output_format:return None
        if output.format not in operator.outputs:return None
        if output.length is not None:values['output_block_length']=output.length
        return values,output

    def _actions(self,state,operator,function):
        ancestors=frozenset(dict(state.assignments)) if function is None else self.ancestors[function.id]
        choices=[]
        for port,formats in operator.inputs.items():
            available=[]
            for token in state.tokens:
                if token.signal.format not in formats:continue
                if not token.completed<=ancestors:continue
                if function:
                    if function.input_format and token.signal.format!=function.input_format:continue
                    required=function.port_dependencies.get(port)
                    if required and required not in token.completed:continue
                    if function.port_dependencies:
                        permitted=(self.ancestors[required]|{required}) if required else frozenset()
                        if not token.completed<=permitted:continue
                available.append(token)
            choices.append(available)
        if not choices or any(not items for items in choices):return []
        actions=[]
        for inputs in product(*choices):
            completed=frozenset().union(*(t.completed for t in inputs))
            # A local conversion is applied once per functional frontier. Further
            # conversion stages require an explicit functional demand/parameters.
            if function is None and any(step.function_id is None and step.operator.id==operator.id
                    and step.output.completed==completed for step in state.steps):continue
            if function and not set(function.predecessors)<=completed:continue
            if function and len(operator.inputs)>1 and len(function.predecessors)>1 and not function.port_dependencies:
                self.fail('NEEDS_PARAMETERS','multi-input dependency edges need target_port names',function.id);continue
            result=self._parameters(operator,function,[t.signal for t in inputs])
            if result is None:continue
            parameters,output=result
            node_id=function.id if function else 'adapter-'+str(len(state.steps)+1).zfill(2)
            function_id=function.id if function else None
            signature=(operator.id,json.dumps(parameters,sort_keys=True),tuple(t.id for t in inputs))
            if any((step.operator.id,json.dumps(step.node.parameters,sort_keys=True),tuple(step.inputs.values()))==signature for step in state.steps):continue
            node=SpecificationNode(node_id,node_id,operator.capability,
                {port:t.signal.format for port,t in zip(operator.inputs,inputs)}, {'out':output.format},parameters,(function_id or 'adapter').split(':')[0])
            key=(operator.id,json.dumps(parameters,sort_keys=True),tuple(node.inputs.items()),output.format)
            if key in self.candidate_cache:self.statistics['candidate_cache_hits']+=1;candidate=self.candidate_cache[key]
            else:
                self.statistics['candidate_checks']+=1
                try:candidate=validate_candidate(self.task,node,operator.cid,self.interfaces,self.protocols)
                except (MatchRuleError,TypeError,ValueError) as exc:
                    self.fail(getattr(exc,'code','INVALID_PARAMETERS'),str(exc),function_id);candidate=None
                self.candidate_cache[key]=candidate
            if candidate is None:continue
            compatible=True
            steps={step.id:step for step in state.steps}
            for port,token in zip(operator.inputs,inputs):
                if token.producer is None:continue
                source=steps[token.producer]
                edgekey=(source.operator.id,json.dumps(source.node.parameters,sort_keys=True),source.output.signal,
                    operator.id,json.dumps(parameters,sort_keys=True),port)
                if edgekey in self.edge_cache:
                    self.statistics['compatibility_cache_hits']+=1;error=self.edge_cache[edgekey]
                else:
                    self.statistics['compatibility_checks']+=1
                    left=validate_candidate(self.task,source.node,source.binding.cid,self.interfaces,self.protocols)
                    error=validate_connection(self.task,SpecificationEdge(source.id,'out',node_id,port),source.node,node,
                        left.component,candidate.component,source.binding,candidate.binding)
                    self.edge_cache[edgekey]=error
                if error:self.fail('INTERFACE_MISMATCH',error,function_id);compatible=False;break
            if not compatible:continue
            # Automatic adapters must actually change a signal state and remain bounded.
            if function is None and any(output==t.signal for t in inputs):continue
            token=Token(node_id+':out',output,completed|({function_id} if function_id else set()),node_id)
            step=Step(node_id,function_id,operator,node,candidate.binding,dict(zip(operator.inputs,(t.id for t in inputs))),token)
            assignments=state.assignments+((function_id,token.id),) if function_id else state.assignments
            actions.append(State(state.tokens+(token,),state.steps+(step,),assignments,state.cost+operator.cost))
        return actions

    def expand(self,state):
        assigned=dict(state.assignments)
        self.statistics['forward_checks']+=1
        if any(not self.domains[f] for f in self.functions if f not in assigned):return []
        ready=[f for f in self.functions.values() if f.id not in assigned and set(f.predecessors)<=assigned.keys()]
        domains=[]
        for f in ready:
            actions=[child for operator in self.domains[f.id] for child in self._actions(state,operator,f)]
            if actions:domains.append((len(actions),-sum(f.id in x.predecessors for x in self.functions.values()),f.id,actions))
        actions=min(domains,key=lambda row:row[:3])[3] if domains else []
        # Explore conversion alternatives even when a direct implementation works.
        # Otherwise MRV could silently discard a cheaper compatible topology.
        adapters=[]
        for operator in self.operators:
            if not operator.automatic:continue
            for child in self._actions(state,operator,None):
                if child.tokens[-1].signal not in [t.signal for t in state.tokens]:adapters.append(child)
        return actions+adapters

    def finalize(self,state):
        from matching.matcher import StaticMatchResult,_build_execution_graph
        tokens={t.id:t for t in state.tokens}
        edges=[];boundaries=[]
        for step in state.steps:
            for port,identifier in step.inputs.items():
                token=tokens[identifier]
                if token.producer:edges.append(SpecificationEdge(token.producer,'out',step.id,port))
                else:boundaries.append(dict(direction='source',node_id=step.id,port=port,format=token.signal.format,business_node_id=token.source))
        consumed={p for function in self.functions.values() for p in function.predecessors}
        for function_id,identifier in state.assignments:
            if function_id in consumed:continue
            token=tokens[identifier]
            boundaries.append(dict(direction='sink',node_id=token.producer,port='out',format=token.signal.format,business_node_id=function_id))
        expected=tuple(self.normalized['S']['expected_output'])
        # Output names are semantic contracts, not arbitrary labels for any sink.
        formats={b['format'] for b in boundaries if b['direction']=='sink'}
        output_contracts={'spectrum_psd':{'spectrum/psd'},
            'speech_transcript':{'text/transcript'},'satellite_identity':{'classification/satellite_identity'},
            'target_detections':{'detections/targets'}}
        uncovered=[name for name in expected if not formats.intersection(output_contracts.get(name,{name}))]
        if uncovered:
            self.fail('UNCOVERED_OUTPUT','no terminal component produces requested outputs: '+', '.join(uncovered))
            return None
        graph=SpecificationGraph(tuple(step.node for step in state.steps),tuple(edges),tuple(boundaries),
            'demand_composition','1.0','component_dag',{},(),expected,())
        task=replace(self.task,G=graph)
        # Every selected candidate and edge was already validated during expansion.
        # Do not rerun a separate first-feasible matcher and replace its selection.
        bindings={step.id:step.binding for step in state.steps}
        try:execution_graph=_build_execution_graph(task,bindings)
        except ValueError as exc:self.fail('INCOMPLETE_DAG',str(exc));return None
        return StaticMatchResult(task.task_id,'MATCHED',bindings,tuple((e.source,e.target) for e in edges),(),
            refined_graph=graph.to_dict(),execution_graph=execution_graph,candidate_execution_graphs=(execution_graph,),
            candidate_bindings=(bindings,),specification_task=task,candidates_exhaustive=True)

    @staticmethod
    def solution_key(result):
        return json.dumps(dict(nodes=result.execution_graph['nodes'],edges=result.execution_graph['edges']),sort_keys=True)
