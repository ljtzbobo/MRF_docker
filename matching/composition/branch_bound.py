"""Bounded depth-first branch-and-bound; interrupted search never proves optimum."""
from time import monotonic
import math

from .models import SearchOutcome


def search(problem,*,max_solutions=4,max_states=10000,max_nodes=24,time_limit_ms=2000):
    for name,value in [('max_solutions',max_solutions),('max_states',max_states),('max_nodes',max_nodes)]:
        if type(value) is not int or value<1:raise ValueError(name+' must be positive')
    if type(time_limit_ms) not in (int,float) or not math.isfinite(time_limit_ms) or time_limit_ms<=0:
        raise ValueError('time_limit_ms must be positive and finite')
    started=monotonic();solutions=[];seen=set()
    stats=dict(algorithm='backtracking_branch_and_bound',explored_states=0,pruned_by_bound=0,
        pruned_by_constraints=0,pruned_by_node_limit=0,complete_graphs=0,
        optimality_proven=False,search_complete=True,termination_reason='exhausted',
        cost_basis='sum_of_declared_nonnegative_operator_costs; default=component_count',
        optimality_scope='registered_operators_and_declared_parameters; one_automatic_conversion_per_functional_frontier',
        qos_verified=False,hardware_placement='module_three')
    root_bound=problem.lower_bound(problem.initial);hard_stop=False

    def visit(state):
        nonlocal hard_stop
        if hard_stop:return
        if stats['explored_states']>=max_states or (monotonic()-started)*1000>=time_limit_ms:
            stats['search_complete']=False
            hard_stop=True
            stats['termination_reason']='state_limit' if stats['explored_states']>=max_states else 'time_limit'
            return
        stats['explored_states']+=1
        bound=problem.lower_bound(state)
        if len(solutions)==max_solutions and bound>=solutions[-1][0]:
            stats['pruned_by_bound']+=1;return
        if problem.complete(state):
            result=problem.finalize(state)
            if result is None:stats['pruned_by_constraints']+=1;return
            key=problem.solution_key(result)
            if key in seen:return
            seen.add(key);stats['complete_graphs']+=1
            if 'initial_solution_cost' not in stats:stats['initial_solution_cost']=state.cost
            solutions.append((state.cost,result))
            solutions.sort(key=lambda item:(item[0],problem.solution_key(item[1])))
            del solutions[max_solutions:]
            return
        if len(state.steps)>=max_nodes:
            stats['pruned_by_node_limit']+=1
            stats['search_complete']=False;stats['termination_reason']='node_limit'
            return
        successors=problem.expand(state)
        if not successors:stats['pruned_by_constraints']+=1
        for child in sorted(successors,key=lambda child:problem.lower_bound(child)):
            visit(child)

    visit(problem.initial)
    stats.update(problem.statistics)
    stats['elapsed_ms']=(monotonic()-started)*1000
    stats['best_cost']=solutions[0][0] if solutions else None
    stats['best_lower_bound']=stats['best_cost'] if solutions and stats['search_complete'] else root_bound
    stats['optimality_proven']=bool(solutions) and stats['search_complete']
    stats['status']='OPTIMAL' if stats['optimality_proven'] else 'FEASIBLE_SEARCH_LIMIT' if solutions else 'INFEASIBLE' if stats['search_complete'] else 'SEARCH_LIMIT'
    return SearchOutcome([result for _,result in solutions],stats)
