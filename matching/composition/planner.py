"""Convert business requirements into a bounded component-DAG assembly problem."""
from dataclasses import replace

from components.catalog import default_capability_map
from matching.legacy_refinement import refine_business_task
from .models import Function
from .operators import load_operators
from .problem import CompositionProblem
from .branch_bound import search


ROLE_CAPABILITIES={
    'downconvert_iq':'digital_down_conversion','filter_signal':'fir_filter',
    'estimate_spectrum':'fast_spectrum_sensing','recognize_speech':'speech_recognition',
    'identify_satellite':'satellite_identity_classification','detect_targets':'radar_matrix_target_detection',
}
BOUNDARIES={'acquire_signal','acquire_iq_samples','deliver_spectrum','deliver_text','deliver_identity','deliver_detections'}


def requirements(normalized):
    request=normalized['S']['business_parameters']
    explicit=request.get('functional_dag')
    graph=normalized['G']
    if not explicit and normalized['S']['task_type']=='spectrum_analysis' and any(n['role']=='analyze_spectrum' for n in graph['nodes']):
        # Legacy business roles are requirements, not implementation recipes.
        # Explicit filtering/translation parameters become mandatory functions.
        p=request.get('processing',{});result=[]
        # Retained legacy business graphs must still satisfy their declared dependencies.
        from task_repr.parsing.external_task import RULES
        roles=RULES['task_types']['spectrum_analysis']['branches']['main']
        expected_nodes={f'main-{i:02d}':role for i,role in enumerate(roles,1)}
        expected_edges={(f'main-{i:02d}',f'main-{i+1:02d}') for i in range(1,len(roles))}
        if {n['id']:n['role'] for n in graph['nodes']}!=expected_nodes or {(e['source'],e['target']) for e in graph['edges']}!=expected_edges:
            raise ValueError('business graph differs from its declared task rules; use request.functional_dag for custom dependencies')
        if p.get('decimation',1)!=1 or p.get('nco_frequency_mhz',0)!=0:
            result.append(Function('main-02:ddc','digital_down_conversion'))
        if 'filter_cutoff' in p or 'coefficients' in p:
            result.append(Function('main-03:filter','fir_filter',tuple(f.id for f in result[-1:]),
                {'sample_rate_hz':p['baseband_sample_rate_mhz']*1e6} if 'baseband_sample_rate_mhz' in p else {}))
        result.append(Function('main-03:sensing','fast_spectrum_sensing',tuple(f.id for f in result[-1:]),
            {'sample_rate_hz':p['baseband_sample_rate_mhz']*1e6} if 'baseband_sample_rate_mhz' in p else {}))
        return tuple(result)
    if explicit is not None:
        if {n['id'] for n in explicit['nodes']}!={n['id'] for n in graph['nodes']}:
            raise ValueError('functional DAG and represented G disagree')
        if ({n['id']:n['capability'] for n in explicit['nodes']}!={n['id']:n['role'] for n in graph['nodes']}
            or {(e['source'],e['target']) for e in explicit['edges']}!={(e['source'],e['target']) for e in graph['edges']}):
            raise ValueError('functional DAG and represented G disagree')
        parameters={n['id']:n for n in explicit['nodes']}
    else:parameters={}
    nodes={n['id']:n for n in graph['nodes']}
    if len(nodes)!=len(graph['nodes']):raise ValueError('duplicate functional node ID')
    for edge in graph['edges']:
        if edge['source'] not in nodes or edge['target'] not in nodes:raise ValueError('functional edge references unknown node')
    functional={key:value for key,value in nodes.items() if value['role'] not in BOUNDARIES}
    result=[]
    for identifier,node in functional.items():
        details=parameters.get(identifier,{})
        predecessors=tuple(e['source'] for e in graph['edges'] if e['target']==identifier and e['source'] in functional)
        ports={e['target_port']:e['source'] for e in (explicit or {}).get('edges',[])
               if e['target']==identifier and 'target_port' in e}
        values=dict(details.get('parameters',{}))
        processing=request.get('processing',{})
        if not explicit and node['role'] in ('filter_signal','estimate_spectrum'):
            rate=processing.get('baseband_sample_rate_mhz')
            if rate is not None:values['sample_rate_hz']=rate*1e6
        result.append(Function(identifier,details.get('capability',ROLE_CAPABILITIES.get(node['role'],node['role'])),
            predecessors,values,details.get('input_format'),details.get('output_format'),ports))
    return tuple(result)


def compose_business_task(normalized,*,capability_map=None,max_solutions=4,limits=None):
    from matching.matcher import StaticMatchResult,MatchIssue,_match_fixed
    capability_map=capability_map or default_capability_map()
    s=normalized['S'];request=s['business_parameters']
    is_dynamic=bool(request.get('functional_dag')) or s['task_type']=='spectrum_analysis' or any(
        name in s['expected_output'] for name in ('speech_transcript','satellite_identity')) or s['data_input'].get('format')=='radar/range_pulse_matrix'
    if not is_dynamic and request.get('processing',{}).get('profile'):
        # Compatibility adapter for old, explicitly configured protocol waveforms.
        # New functional DAGs never enter this route or require a profile name.
        result=_match_fixed(refine_business_task(normalized),capability_map,max_solutions=max_solutions)
        return replace(result,composition=dict(mode='explicit_waveform_compatibility',
            algorithm='legacy_contract_matching',optimality_proven=False))
    operators=load_operators(capability_map)
    try:
        functions=requirements(normalized)
        if not functions:raise ValueError('functional DAG has no processing requirements')
        problem=CompositionProblem(normalized,functions,operators,capability_map)
    except (ValueError,TypeError,KeyError) as exc:
        return StaticMatchResult(normalized['task_id'],'NEEDS_PARAMETERS',{},(),(MatchIssue('INVALID_REQUIREMENTS',str(exc)),))
    missing=[f for f in functions if not problem.domains[f.id]]
    if missing:
        return StaticMatchResult(normalized['task_id'],'MISSING_CAPABILITY',{},(),tuple(
            MatchIssue('MISSING_CAPABILITY','no runnable component provides '+f.capability,f.id) for f in missing),
            composition=dict(mode='demand_driven',algorithm='backtracking_branch_and_bound',
                optimality_proven=False,explored_states=0,missing_capabilities=[f.capability for f in missing]))
    options=dict(max_solutions=max_solutions)
    if limits:
        if set(limits)-{'max_states','max_nodes','time_limit_ms'}:raise ValueError('unknown composition search limit')
        options.update(limits)
    outcome=search(problem,**options)
    info={**outcome.statistics,'mode':'demand_driven','candidate_count':len(outcome.solutions),
        'functional_requirements':[dict(id=f.id,capability=f.capability,predecessors=list(f.predecessors)) for f in functions]}
    if not outcome.solutions:
        issues=tuple(MatchIssue(v['code'],v['detail'],v['node_id']) for v in problem.failures.values())
        return StaticMatchResult(normalized['task_id'],'SEARCH_LIMIT' if not info['search_complete'] else
            'INFEASIBLE' if any(i.code=='UNCOVERED_OUTPUT' for i in issues) else
            'NEEDS_PARAMETERS' if any(i.code in ('NEEDS_PARAMETERS','INVALID_PARAMETERS') for i in issues) else 'INFEASIBLE',{},(),
            issues or (MatchIssue('NO_COMPATIBLE_COMPOSITION','no component DAG satisfies the input and functional dependencies'),),composition=info)
    results=tuple(replace(result,composition={**info,'candidate_index':i,
        'candidate_cost':sum(next(o.cost for o in operators if o.cid==binding.cid and o.capability==node.capability)
            for node in result.specification_task.G.nodes for binding in (result.bindings[node.node_id],))}) for i,result in enumerate(outcome.solutions))
    exhaustive=info['search_complete'] and not info['pruned_by_bound'] and info['complete_graphs']<=max_solutions
    return replace(results[0],candidate_compositions=results,candidates_exhaustive=exhaustive)
