"""Configured creation candidates, distinct from live instances or idle containers."""
from components import default_component_catalog
from components.implementations.runtime_registry import RUNTIME_COMPONENT_REGISTRY


def component_inventory(planner, runtime):
    simulated = getattr(runtime, 'execution_mode', 'docker') == 'software_simulation'
    catalog = default_component_catalog()
    images = getattr(runtime, 'images', {})
    cids = set(images) | {p.cid for p in planner.measurements.profiles}
    result = []
    for cid in sorted(cids):
        profiles = [p for p in planner.measurements.profiles if p.cid == cid]
        implementations = {}
        for profile in profiles:
            key = (profile.implementation_id, profile.processor, repr(sorted(profile.parameters.items())))
            entry = implementations.setdefault(key, dict(
                implementation_id=profile.implementation_id, processor=profile.processor,
                parameters=profile.parameters, processing_nodes=[]))
            entry['processing_nodes'].extend(n.node_id for n in planner.nodes.values()
                if profile.applies_to(cid, profile.parameters, n.hardware)
                and n.node_id not in entry['processing_nodes'])
        try:
            name = catalog.get(cid).short_name
        except KeyError:
            name = cid
        status = ('MISSING_RUNTIME' if cid not in RUNTIME_COMPONENT_REGISTRY else
                  'NEEDS_CALIBRATION' if not profiles else
                  'SOFTWARE_REFERENCE' if simulated else
                  'IMAGE_CONFIGURED' if cid in images else 'MISSING_IMAGE')
        result.append(dict(cid=cid, name=name, creation_status=status,
                           image=images.get(cid), image_verified=False,
                           execution_backend='python_cpu_reference' if simulated else 'docker',
                           implementations=list(implementations.values())))
    return result
