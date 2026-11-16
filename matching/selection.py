"""Keep a selected specification DAG, bindings and source boundary mapping together."""
from collections.abc import Mapping,Sized
from dataclasses import replace
from deployment import DeploymentError


def select_composition(match,preview):
    candidates=match.candidate_compositions or (match,)
    evaluated=[]
    for index,candidate in enumerate(candidates):
        task=candidate.specification_task
        if task is None:continue
        plan=preview(task,candidate)
        evaluated.append((index,candidate,plan))
    if not evaluated:raise DeploymentError('no executable component specification was assembled')
    priorities={'FEASIBLE_ESTIMATE':0,'UNVERIFIED_TIME':1,'SEARCH_LIMIT':2,'INFEASIBLE':3,
                'NEEDS_CALIBRATION':4,'NEEDS_TRANSFER_PROFILE':5}
    index,chosen,plan=min(evaluated,key=lambda row:(priorities.get(row[2].status,6),
        row[2].estimated_completion_ms if row[2].estimated_completion_ms is not None else float('inf'),row[0]))
    selection=dict(selected_candidate=index,candidates=[dict(index=i,status=p.status,estimated_completion_ms=p.estimated_completion_ms)
        for i,_,p in evaluated],scope='bounded_module2_candidates_then_module3_placement')
    chosen=replace(chosen,composition={**(chosen.composition or {}),'resource_selection':selection},candidate_compositions=())
    return chosen,plan,selection


class ReboundFrames:
    def __init__(self,frames,original,selected):
        self.frames=frames if isinstance(frames,Sized) else tuple(frames)
        self.original=[b for b in original['boundaries'] if b['direction']=='source']
        self.selected=[b for b in selected['boundaries'] if b['direction']=='source']
    def __len__(self):return len(self.frames)
    def __iter__(self):
        for frame in self.frames:
            # The UI can submit packets using the already selected preview graph.
            original_nodes={b['node_id'] for b in self.original}
            selected_nodes={b['node_id'] for b in self.selected}
            if selected_nodes==set(frame) and not original_nodes<=frame.keys():
                yield frame
                continue
            logical={}
            for boundary in self.original:
                value=frame[boundary['node_id']]
                packet=value[boundary['port']] if isinstance(value,Mapping) else value
                key=(boundary['business_node_id'],boundary['format'])
                if key in logical and logical[key] is not packet:
                    raise DeploymentError('ambiguous independent sources; provide distinct business source identities')
                logical[key]=packet
            result={}
            for boundary in self.selected:
                key=(boundary['business_node_id'],boundary['format'])
                if key not in logical:raise DeploymentError('selected graph requires a different data source contract')
                result.setdefault(boundary['node_id'],{})[boundary['port']]=logical[key]
            yield {key:next(iter(value.values())) if len(value)==1 else value for key,value in result.items()}
