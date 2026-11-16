"""One static candidate validator shared by fixed-DAG matching and synthesis."""
from components.implementations.runtime_registry import create_runtime_component
from .contracts import check_node_ports
from .rules import MatchRuleError,adapt_parameters


def validate_candidate(task,node,cid,interfaces,protocols):
    from .matcher import ComponentBinding,_Candidate,_signal_checks
    parameters=adapt_parameters(task,node,cid)
    interface=interfaces.get(cid)
    if interface is None:raise MatchRuleError('UNSUPPORTED','no OCS for '+cid)
    error=interface.check_required(parameters)
    if error:raise MatchRuleError('NEEDS_PARAMETERS',error)
    for name,value in parameters.items():
        error=interface.check_property(name,value)
        if error:raise MatchRuleError('INVALID_PARAMETERS',error)
    component=create_runtime_component(cid,**parameters)
    error=check_node_ports(node,component,interface,protocols,parameters)
    if error:raise MatchRuleError('INTERFACE_MISMATCH',error)
    candidate=_Candidate(ComponentBinding(cid,parameters),component)
    _signal_checks(task,node,candidate)
    return candidate
