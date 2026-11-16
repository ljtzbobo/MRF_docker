"""Demand assembly, semantic coverage, alternative topology and bounded optimality."""
from dataclasses import replace
from pathlib import Path
import json
import unittest
from types import SimpleNamespace

import numpy as np
from matching import match_task
from matching.composition.branch_bound import search
from matching.composition.models import State
from components.runtime import DataPacket
from task_executor import DAGExecutor
from task_repr import parse_external_task


def task():
    raw=json.loads((Path(__file__).parents[1]/'examples/BusinessRequests/External/07_spectrum_analysis.json').read_text(encoding='utf-8'))
    raw['request']['processing']={'fft_length':16,'overlap':0,'input_sample_rate_mhz':4,'input_block_length':64}
    return raw


class DemandCompositionTests(unittest.TestCase):
    def test_same_goal_uses_direct_input_or_inserts_contract_adapter(self):
        raw=task()
        direct=match_task(raw)
        self.assertEqual(direct.status,'MATCHED',direct.to_dict())
        self.assertEqual([b.cid for b in direct.bindings.values()],['C27'])
        raw['request']['functional_dag']={'nodes':[{'id':'psd','capability':'fast_spectrum_sensing',
            'input_format':'iq/baseband','parameters':{'sample_rate_hz':2e6}}],'edges':[]}
        raw['request']['processing'].update(decimation=2,nco_frequency_mhz=0)
        converted=match_task(raw)
        self.assertEqual(converted.status,'MATCHED',converted.to_dict())
        self.assertEqual([b.cid for b in converted.bindings.values()],['C03','C27'])
        executor=DAGExecutor.from_execution_graph(converted.execution_graph)
        root=next(b['node_id'] for b in converted.execution_graph['boundaries'] if b['direction']=='source')
        result=executor.execute({root:DataPacket(np.ones(64,dtype=complex),4e6,0,0,'time')})
        self.assertEqual(result.sink_outputs['psd'].item_count,16)

    def test_function_dependencies_and_branch_join_are_not_a_chain_recipe(self):
        raw=task();raw['expected_output']=['iq/complex']
        raw['request']['functional_dag']={'nodes':[
            {'id':'left','capability':'fir_filter','parameters':{'coefficients':[1]}},
            {'id':'right','capability':'fir_filter','parameters':{'coefficients':[2]}},
            {'id':'join','capability':'iq_branch_merge','output_format':'iq/complex',
             'parameters':{'gain_left':1,'gain_right':1}}],
            'edges':[{'source':'left','target':'join','target_port':'left'},
                     {'source':'right','target':'join','target_port':'right'}]}
        matched=match_task(raw)
        self.assertEqual(matched.status,'MATCHED',matched.to_dict())
        self.assertEqual(set(matched.edges),{('left','join'),('right','join')})
        packet=DataPacket(np.ones(64,dtype=complex),4e6,0,0,'time')
        result=DAGExecutor.from_execution_graph(matched.execution_graph).execute({'left':packet,'right':packet})
        np.testing.assert_allclose(result.sink_outputs['join'].samples,3)

    def test_output_name_cannot_make_wrong_component_fulfill_goal(self):
        raw=task();raw['request']['functional_dag']={'nodes':[{'id':'filter','capability':'fir_filter',
            'parameters':{'coefficients':[1]}}],'edges':[]}
        matched=match_task(raw)
        self.assertEqual(matched.status,'INFEASIBLE')
        self.assertTrue(any(i.code=='UNCOVERED_OUTPUT' for i in matched.unresolved))

    def test_cycles_and_component_bindings_are_not_functional_requirements(self):
        raw=task();raw['request']['functional_dag']={'nodes':[{'id':'a','capability':'fir_filter'}],
            'edges':[{'source':'a','target':'a'}]}
        with self.assertRaisesRegex(ValueError,'cycle'):parse_external_task(raw)
        raw['request']['functional_dag']['edges']=[]
        raw['request']['functional_dag']['nodes'][0]['cid']='C05'
        with self.assertRaisesRegex(ValueError,'CID'):parse_external_task(raw)

    def test_state_limit_is_not_reported_as_infeasible_or_optimal(self):
        matched=match_task(task(),composition_limits={'max_states':1})
        self.assertEqual(matched.status,'SEARCH_LIMIT')
        self.assertFalse(matched.composition['optimality_proven'])

    def test_topology_alternatives_are_kept_with_their_own_graphs(self):
        raw=task();raw['request']['functional_dag']={'nodes':[{'id':'psd','capability':'fast_spectrum_sensing'}],'edges':[]}
        raw['request']['processing'].update(decimation=2,nco_frequency_mhz=0)
        matched=match_task(raw)
        self.assertEqual(matched.status,'MATCHED')
        self.assertEqual([len(c.bindings) for c in matched.candidate_compositions],[1,2])
        for candidate in matched.candidate_compositions:
            self.assertEqual({n.node_id for n in candidate.specification_task.G.nodes},set(candidate.bindings))
            self.assertEqual({n['id'] for n in candidate.execution_graph['nodes']},set(candidate.bindings))
            self.assertTrue(candidate.candidates_exhaustive)

    def test_bad_contract_metadata_is_a_diagnostic(self):
        raw=task();raw['request']['functional_dag']={'nodes':[{'id':'psd','capability':'fast_spectrum_sensing',
            'parameters':{'sample_rate_hz':'bad'}}],'edges':[]}
        self.assertEqual(match_task(raw).status,'NEEDS_PARAMETERS')

    def test_selected_topology_rebinds_source_packets_before_execution(self):
        from matching.selection import select_composition,ReboundFrames
        raw=task();raw['request']['functional_dag']={'nodes':[{'id':'psd','capability':'fast_spectrum_sensing'}],'edges':[]}
        raw['request']['processing'].update(decimation=2,nco_frequency_mhz=0)
        matched=match_task(raw)
        chosen,_,selection=select_composition(matched,lambda spec,candidate:SimpleNamespace(
            status='FEASIBLE_ESTIMATE' if len(spec.G.nodes)==2 else 'INFEASIBLE',estimated_completion_ms=1))
        self.assertEqual(selection['selected_candidate'],1)
        packet=DataPacket(np.ones(64,dtype=complex),4e6,0,0,'time')
        frames=ReboundFrames([{'psd':packet}],matched.execution_graph,chosen.execution_graph)
        result=DAGExecutor.from_execution_graph(chosen.execution_graph).execute(next(iter(frames)))
        self.assertEqual(result.sink_outputs['psd'].item_count,16)
        self.assertEqual(result.sink_outputs['psd'].sample_rate,2e6)

    def test_join_can_mix_an_internal_edge_with_an_external_input(self):
        raw=task();raw['expected_output']=['iq/complex']
        raw['request']['functional_dag']={'nodes':[
            {'id':'filter','capability':'fir_filter','parameters':{'coefficients':[2]}},
            {'id':'join','capability':'iq_branch_merge','output_format':'iq/complex',
             'parameters':{'gain_left':1,'gain_right':1}}],
            'edges':[{'source':'filter','target':'join','target_port':'left'}]}
        matched=match_task(raw)
        self.assertEqual(matched.status,'MATCHED',matched.to_dict())
        packet=DataPacket(np.ones(64,dtype=complex),4e6,0,0,'time')
        sources={}
        for b in matched.execution_graph['boundaries']:
            if b['direction']=='source':sources.setdefault(b['node_id'],{})[b['port']]=packet
        result=DAGExecutor.from_execution_graph(matched.execution_graph).execute(sources)
        np.testing.assert_allclose(result.sink_outputs['join'].samples,3)

    def test_missing_component_is_reported_by_capability(self):
        raw=task();raw['request']['functional_dag']={'nodes':[{'id':'asr','capability':'speech_recognition'}],'edges':[]}
        matched=match_task(raw)
        self.assertEqual(matched.status,'MISSING_CAPABILITY')
        self.assertIn('speech_recognition',matched.unresolved[0].detail)


class TinyProblem:
    """Exhaustively enumerable costs: (3, 1, 4) x (5, 2)."""
    initial=State(())
    statistics={}
    costs=((3,1,4),(5,2))
    def lower_bound(self,state):return state.cost+sum(min(c) for c in self.costs[len(state.steps):])
    def complete(self,state):return len(state.steps)==2
    def expand(self,state):return [replace(state,steps=state.steps+(cost,),cost=state.cost+cost) for cost in self.costs[len(state.steps)]]
    def finalize(self,state):return state
    def solution_key(self,state):return repr(state.steps)


class BranchBoundTests(unittest.TestCase):
    def test_bound_matches_exhaustive_oracle_and_prunes(self):
        result=search(TinyProblem(),max_solutions=1)
        self.assertEqual(result.solutions[0].cost,min(a+b for a in (3,1,4) for b in (5,2)))
        self.assertTrue(result.statistics['optimality_proven'])
        self.assertGreater(result.statistics['pruned_by_bound'],0)
    def test_best_k_and_interruption(self):
        result=search(TinyProblem(),max_solutions=2)
        self.assertEqual([s.cost for s in result.solutions],[3,5])
        limited=search(TinyProblem(),max_solutions=2,max_states=3)
        self.assertTrue(limited.solutions)
        self.assertFalse(limited.statistics['optimality_proven'])
        self.assertEqual(limited.statistics['status'],'FEASIBLE_SEARCH_LIMIT')


if __name__=='__main__':unittest.main()
