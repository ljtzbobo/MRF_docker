"""Reference core identity, indivisible regions and multi-dimensional competition."""
from dataclasses import asdict, replace
import itertools
import json
from pathlib import Path
import tempfile
import unittest

from deployment.simulation import load_simulation_manager
from examples.heterogeneous_simulation_demo import CONFIG, sample_task
from resources.measurements import (FPGA_FIELDS, LEGACY_USAGE_FIELDS, MeasurementCatalog, PhysicalUsage,
                                    load_measurements, save_measurements)
from resources.reference_devices import FPGA_REGION, FPGA_TOTAL
from resources.simulation import load_resource_simulation


class ReferenceModelTests(unittest.TestCase):
    def setUp(self):
        self.manager = load_simulation_manager(CONFIG)
        _, self.task, self.match = sample_task()
        self.planner = self.manager.scheduler.placement_planner
        self.state = self.manager.scheduler.placement_baseline

    def test_exact_core_and_region_topology_and_device_ceilings(self):
        cores = [n for n in self.planner.nodes.values() if n.processor == 'dsp']
        regions = [n for n in self.planner.nodes.values() if n.processor == 'fpga']
        self.assertEqual({n.unit_index for n in cores}, set(range(4)))
        self.assertEqual({n.unit_index for n in regions}, set(range(4)))
        self.assertEqual({n.device_id for n in cores}, {'66AK2H06-0'})
        for core in cores:
            self.assertEqual(core.hardware.capacity.dsp_compute_mcycles_per_s, 1200)
            self.assertEqual(core.hardware.capacity.dsp_local_memory_bytes, 1024**2)
        for field in FPGA_FIELDS:
            self.assertEqual(sum(getattr(n.hardware.capacity, field) for n in regions), FPGA_REGION[field] * 4)
            self.assertLessEqual(FPGA_REGION[field] * 4, FPGA_TOTAL[field])
        for profile in self.planner.measurements.profiles:
            self.assertEqual(profile.demand.fpga_units, 0)
            self.assertEqual(profile.demand.dsp_units, 0)

    def test_all_algorithms_respect_each_fpga_dimension(self):
        for field in FPGA_FIELDS:
            used = dict(self.state.node_used)
            for key, node in self.planner.nodes.items():
                if node.processor == 'fpga':
                    used[key] = PhysicalUsage(**{field: getattr(node.hardware.capacity, field)})
            state = replace(self.state, node_used=used)
            for algorithm in ('exact', 'chain_dp', 'heft'):
                with self.subTest(field=field, algorithm=algorithm):
                    plan = self.planner.plan(self.task, self.match, state, algorithm=algorithm)
                    self.assertEqual(plan.status, 'FEASIBLE_ESTIMATE')
                    self.assertNotIn('fpga', {a.processor for a in plan.allocations})

    def test_all_algorithms_respect_each_dsp_core_compute_and_local_memory(self):
        for field in ('dsp_compute_mcycles_per_s', 'dsp_local_memory_bytes'):
            used = dict(self.state.node_used)
            for key, node in self.planner.nodes.items():
                if node.processor == 'dsp':
                    used[key] = PhysicalUsage(**{field: getattr(node.hardware.capacity, field)})
            for algorithm in ('exact', 'chain_dp', 'heft'):
                plan = self.planner.plan(self.task, self.match, replace(self.state, node_used=used), algorithm=algorithm)
                self.assertEqual(plan.status, 'FEASIBLE_ESTIMATE')
                self.assertNotIn('dsp', {a.processor for a in plan.allocations})

    def test_component_cannot_pool_spare_resources_across_regions(self):
        node = self.task.G.nodes[-1]
        task = replace(self.task, G=replace(self.task.G, nodes=(node,), edges=()))
        match = replace(self.match, bindings={node.node_id: self.match.bindings[node.node_id]},
                        edges=(), candidate_bindings=())
        profiles = tuple(replace(p, demand=replace(p.demand, fpga_lut=FPGA_REGION['fpga_lut'] + 1))
                         for p in self.planner.measurements.profiles if p.processor == 'fpga')
        self.planner.measurements = MeasurementCatalog(profiles)
        for algorithm in ('exact', 'chain_dp', 'heft'):
            plan = self.planner.plan(task, match, self.state, algorithm=algorithm)
            self.assertNotEqual(plan.status, 'FEASIBLE_ESTIMATE')
            self.assertFalse(plan.allocations)

    def test_exact_and_dp_match_independent_enumeration(self):
        ids = tuple(self.match.bindings)
        options = [tuple((key, p) for key, host in self.planner.nodes.items()
                         for p in self.planner.measurements.candidates(binding.cid, binding.parameters, host.hardware))
                   for binding in self.match.bindings.values()]
        best = float('inf')
        frames = 4
        for choices in itertools.product(*options):
            if any(sum(getattr(p.demand, field) for host, p in choices if host == key)
                   + getattr(self.state.node_used[key], field) > getattr(node.hardware.capacity, field)
                   for key, node in self.planner.nodes.items() for field in PhysicalUsage.__dataclass_fields__):
                continue
            assignment = dict(zip(ids, choices))
            link_usage = {key: 0 for key in self.planner.links}
            communication = 0
            for edge in self.task.G.edges:
                left, right = assignment[edge.source][0], assignment[edge.target][0]
                if left == right: continue
                pair = (left, right)
                link = self.planner.links[pair]
                transfer = self.planner.transfers[(edge.source, edge.target)]
                link_usage[pair] += transfer.reserved_bytes_per_s
                communication += link.latency_ms + transfer.bytes_per_call * 1000 / transfer.reserved_bytes_per_s
            if any(value + self.state.link_used[key] > self.planner.links[key].capacity_bytes_per_s
                   for key, value in link_usage.items()): continue
            cost = sum(p.setup_p95_ms for _, p in choices) + frames * (
                sum(p.runtime_p95_ms for _, p in choices) + communication)
            best = min(best, cost)
        for algorithm in ('exact', 'chain_dp'):
            plan = self.planner.plan(self.task, self.match, self.state, algorithm=algorithm, frame_count=frames)
            self.assertTrue(plan.optimality_proven)
            self.assertAlmostEqual(plan.estimated_completion_ms, best)
            for allocation in plan.allocations:
                if allocation.processor == 'dsp': self.assertIsNotNone(allocation.core_id)
                if allocation.processor == 'fpga': self.assertIsNotNone(allocation.region_id)

    def test_extended_profiles_round_trip_and_legacy_loads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profiles.json'
            save_measurements(path, self.planner.measurements)
            self.assertEqual(json.loads(path.read_text())['schema_version'], 2)
            self.assertEqual(load_measurements(path), self.planner.measurements)
            row = asdict(next(p for p in self.planner.measurements.profiles if p.processor == 'cpu'))
            row['demand'] = {key: row['demand'][key] for key in LEGACY_USAGE_FIELDS}
            path.write_text(json.dumps(dict(schema_version=1, profiles=[row])), encoding='utf-8')
            legacy = load_measurements(path)
            self.assertEqual(legacy.profiles[0].demand.fpga_lut, 0)

    def test_warm_state_retains_local_memory_and_fpga_fabric_until_evicted(self):
        scheduler = self.manager.scheduler
        plan = scheduler.submit_placement(self.task, self.match, frame_count=4)
        scheduler.complete_placement(self.task.task_id, keep_warm=True)
        used = scheduler.placement_snapshot()['node_used']
        for node_id in self.planner.nodes:
            for field in ('dsp_local_memory_bytes',) + FPGA_FIELDS:
                expected = getattr(self.state.node_used[node_id], field) + sum(
                    getattr(a.demand, field) for a in plan.allocations if a.processing_node_id == node_id)
                self.assertEqual(used[node_id][field], expected)
            self.assertEqual(used[node_id]['dsp_compute_mcycles_per_s'],
                             self.state.node_used[node_id].dsp_compute_mcycles_per_s)
        for allocation in plan.allocations:
            scheduler.evict_placement_warm(allocation.container_unit_id)
        self.assertEqual(scheduler.placement_snapshot()['node_used'],
                         {key: asdict(value) for key, value in self.state.node_used.items()})

    def test_reference_config_rejects_duplicate_or_oversized_units(self):
        for kind in ('duplicate_core', 'local_memory', 'compute', 'region', 'legacy_slots'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                raw = json.loads(CONFIG.read_text(encoding='utf-8'))
                raw['profiles'] = str(CONFIG.parent / raw['profiles'])
                if kind == 'duplicate_core': raw['nodes'][2]['unit_index'] = 0
                if kind == 'local_memory': raw['nodes'][1]['capacity']['dsp_local_memory_bytes'] += 1
                if kind == 'compute': raw['nodes'][1]['capacity']['dsp_compute_mcycles_per_s'] += 1
                if kind == 'region': raw['nodes'][5]['capacity']['fpga_lut'] += 1
                if kind == 'legacy_slots': raw['nodes'][5]['capacity']['fpga_units'] = 1
                path = Path(directory) / 'config.json'
                path.write_text(json.dumps(raw), encoding='utf-8')
                with self.assertRaises(ValueError): load_resource_simulation(path)


if __name__ == '__main__':
    unittest.main()
