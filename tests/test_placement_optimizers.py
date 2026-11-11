"""Optimizer comparisons and regressions for cumulative physical reservations."""

from dataclasses import replace
import itertools
import tempfile
from pathlib import Path
import unittest

from experiments.resource_allocation.benchmark import make_scenario
from resources.measurements import (MeasurementCatalog, PhysicalUsage, load_measurements,
                                    save_measurements)
from resources.placement import EdgeTransfer, NetworkLink, PlacementPlanner, PlacementState
from scheduler import TaskScheduler


def fixture(count=5, seed=1, shape="chain"):
    return make_scenario(dict(name="test", nodes=count, hosts=2, seed=seed, shape=shape, frames=1))


class PlacementOptimizerTests(unittest.TestCase):
    def test_dp_matches_exhaustive_oracle_on_small_chains(self):
        for seed in range(12):
            with self.subTest(seed=seed):
                task, match, planner, state = fixture(count=5 if seed < 9 else 8, seed=seed)
                # Independent enumeration and serial cost, without using problem.evaluate.
                options = [tuple((host, profile) for host, node in planner.nodes.items()
                                 for profile in planner.measurements.candidates(
                                     match.bindings[key].cid, match.bindings[key].parameters,
                                     node.hardware)) for key in match.bindings]
                costs = []
                frames = seed + 1
                ids = tuple(match.bindings)
                for choices in itertools.product(*options):
                    selected = dict(zip(ids, choices))
                    if any(sum(getattr(profile.demand, field) for host, profile in choices if host == key)
                           + getattr(state.node_used[key], field) > getattr(node.hardware.capacity, field)
                           for key, node in planner.nodes.items() for field in PhysicalUsage.__dataclass_fields__):
                        continue
                    rates = {pair: 0 for pair in planner.links}
                    transfer_ms = 0
                    for edge in task.G.edges:
                        left, right = selected[edge.source][0], selected[edge.target][0]
                        if left != right:
                            transfer = planner.transfers[(edge.source, edge.target)]
                            rates[(left, right)] += transfer.reserved_bytes_per_s
                            transfer_ms += planner.links[(left, right)].latency_ms + transfer.bytes_per_call * 1000 / transfer.reserved_bytes_per_s
                    if any(rates[pair] + state.link_used[pair] > link.capacity_bytes_per_s
                           for pair, link in planner.links.items()):
                        continue
                    costs.append(sum(profile.setup_p95_ms for _, profile in choices) + frames * (
                        sum(profile.runtime_p95_ms for _, profile in choices) + transfer_ms))
                optimum = min(costs)
                for algorithm in ("exact", "chain_dp"):
                    plan = planner.plan(task, match, state, algorithm=algorithm, frame_count=frames)
                    self.assertEqual(plan.status, "FEASIBLE_ESTIMATE")
                    self.assertAlmostEqual(plan.estimated_completion_ms, optimum)
                    self.assertTrue(plan.optimality_proven)

    def test_serial_branch_cost_counts_every_node_setup_and_frame(self):
        task, match, planner, state = fixture(count=4, shape="dag")
        profiles = tuple(replace(profile, runtime_p95_ms=10, setup_p95_ms=2)
                         for profile in planner.measurements.profiles)
        planner.measurements = MeasurementCatalog(profiles)
        fixed = {key: "host-0" for key in match.bindings}
        for algorithm in ("exact", "heft"):
            plan = planner.plan(task, match, state, fixed, algorithm=algorithm, frame_count=3)
            self.assertEqual(plan.estimated_completion_ms, 8 + 3 * 40)
            self.assertIn("not_task_p95", plan.estimation_basis)
        self.assertEqual(planner.plan(task, match, state, algorithm="chain_dp").status,
                         "UNSUPPORTED_GRAPH")

    def test_heft_feasible_cost_cannot_beat_proven_exact_on_dags(self):
        for shape in ("chain", "dag"):
            for seed in range(6):
                task, match, planner, state = fixture(count=7, seed=seed, shape=shape)
                exact = planner.plan(task, match, state)
                heuristic = planner.plan(task, match, state, algorithm="heft")
                self.assertEqual(heuristic.status, "FEASIBLE_ESTIMATE")
                self.assertGreaterEqual(heuristic.estimated_completion_ms, exact.estimated_completion_ms)
                self.assertFalse(heuristic.optimality_proven)

    def test_dp_keeps_labels_with_different_resource_histories(self):
        task, match, planner, state = fixture(count=3)
        # A cheap large first implementation blocks the final mandatory host-0 node.
        # A DP retaining only the cheapest prefix at the same last host loses the solution.
        ids = tuple(match.bindings)
        profiles = []
        for index, key in enumerate(ids):
            base = next(p for p in planner.measurements.profiles if p.cid == match.bindings[key].cid)
            if index == 0:
                profiles.extend((replace(base, implementation_id="large", demand=PhysicalUsage(250, 1024), runtime_p95_ms=1),
                                 replace(base, implementation_id="small", demand=PhysicalUsage(50, 1024), runtime_p95_ms=5)))
            else:
                profiles.append(replace(base, demand=PhysicalUsage(100, 1024), runtime_p95_ms=1))
        planner.measurements = MeasurementCatalog(tuple(profiles))
        fixed = {key: "host-0" for key in ids}
        dp = planner.plan(task, match, state, fixed, algorithm="chain_dp")
        exact = planner.plan(task, match, state, fixed)
        self.assertEqual(dp.status, "FEASIBLE_ESTIMATE")
        self.assertEqual(dp.allocations[0].implementation_id, "small")
        self.assertEqual(dp.estimated_completion_ms, exact.estimated_completion_ms)

    def test_all_algorithms_share_capacity_links_payload_and_baseline_checks(self):
        for algorithm in ("exact", "chain_dp", "heft"):
            for problem_kind in ("cpu", "memory", "bandwidth", "dsp", "fpga", "link", "payload", "missing-link"):
                with self.subTest(algorithm=algorithm, constraint=problem_kind):
                    task, match, planner, state = fixture(count=3)
                    ids = tuple(match.bindings)
                    fixed = {key: "host-0" for key in ids}
                    if problem_kind in ("cpu", "memory", "bandwidth", "dsp", "fpga"):
                        field = dict(cpu="cpu_millicores", memory="memory_bytes", bandwidth="bandwidth_bytes_per_s",
                                     dsp="dsp_units", fpga="fpga_units")[problem_kind]
                        host = planner.nodes["host-0"]
                        # Keep measurements physically consistent with processor type.
                        if problem_kind in ("dsp", "fpga"):
                            processor = problem_kind
                            usage = PhysicalUsage(1, 1, 1, int(processor == "dsp"), int(processor == "fpga"))
                            planner.measurements = MeasurementCatalog(tuple(
                                replace(p, processor=processor, demand=usage) for p in planner.measurements.profiles))
                        total = sum(getattr(p.demand, field) for p in planner.measurements.profiles
                                    if p.hardware_id == host.hardware.hardware_id)
                        capacity = getattr(host.hardware.capacity, field)
                        state.node_used["host-0"] = replace(PhysicalUsage(), **{field: capacity - total + 1})
                    else:
                        fixed[ids[1]] = "host-1"
                        if problem_kind == "link":
                            state.link_used[("host-0", "host-1")] = 9500
                        elif problem_kind == "payload":
                            pair = (ids[0], ids[1])
                            planner.transfers[pair] = EdgeTransfer(*pair, 5000, 1000)
                        elif problem_kind == "missing-link":
                            del planner.links[("host-0", "host-1")]
                            del state.link_used[("host-0", "host-1")]
                    plan = planner.plan(task, match, state, fixed, algorithm=algorithm)
                    self.assertEqual(plan.status, "INFEASIBLE")
                    self.assertFalse(plan.allocations)

    def test_heft_backtracks_when_greedy_first_choice_blocks_later_node(self):
        task, match, planner, state = fixture(count=2)
        ids = tuple(match.bindings)
        profiles = []
        for profile in planner.measurements.profiles:
            if profile.hardware_id == "board-0":
                profiles.append(replace(profile, demand=PhysicalUsage(200, 1024), runtime_p95_ms=1))
            elif profile.cid == match.bindings[ids[0]].cid:
                profiles.append(replace(profile, runtime_p95_ms=10))
        planner.measurements = MeasurementCatalog(tuple(profiles))
        plan = planner.plan(task, match, state, algorithm="heft")
        self.assertEqual(plan.status, "FEASIBLE_ESTIMATE")
        self.assertEqual([a.processing_node_id for a in plan.allocations], ["host-1", "host-0"])
        self.assertFalse(plan.optimality_proven)

    def test_search_limit_and_unknown_algorithm_are_explicit(self):
        task, match, planner, state = fixture()
        for algorithm in ("exact", "chain_dp", "heft"):
            plan = planner.plan(task, match, state, algorithm=algorithm, max_assignments=1)
            self.assertEqual(plan.status, "SEARCH_LIMIT")
            self.assertFalse(plan.optimality_proven)
        for kwargs in (dict(algorithm="unknown"), dict(frame_count=0), dict(frame_count=True)):
            with self.assertRaises(ValueError):
                planner.plan(task, match, state, **kwargs)

    def test_scheduler_forwards_strategy_frames_and_releases_reservations(self):
        task, match, planner, _ = fixture()
        for algorithm in ("chain_dp", "heft"):
            scheduler = TaskScheduler(placement_planner=planner)
            before = scheduler.placement_snapshot()
            preview = scheduler.preview_placement(task, match, algorithm=algorithm, frame_count=7)
            self.assertEqual(scheduler.placement_snapshot(), before)
            plan = scheduler.submit_placement(task, match, algorithm=algorithm, frame_count=7)
            self.assertEqual(plan.estimated_completion_ms, preview.estimated_completion_ms)
            self.assertEqual(plan.algorithm, algorithm)
            self.assertEqual(plan.to_dict()["frame_count"], 7)
            scheduler.complete_placement(task.task_id)
            self.assertEqual(scheduler.placement_snapshot()["node_used"], before["node_used"])

    def test_heft_deadline_miss_does_not_claim_global_infeasibility(self):
        task, match, planner, state = fixture()
        task = replace(task, Q={"deadline_ms": 0.1})
        self.assertEqual(planner.plan(task, match, state, algorithm="heft").status, "HEURISTIC_FAILED")
        self.assertEqual(planner.plan(task, match, state).status, "INFEASIBLE")

    def test_measurement_scope_round_trip_and_old_catalog_defaults(self):
        _, _, planner, _ = fixture()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "measurements.json"
            save_measurements(path, planner.measurements)
            self.assertEqual(load_measurements(path), planner.measurements)
            import json
            raw = json.loads(path.read_text())
            for row in raw["profiles"]:
                row.pop("timing_scope")
                row.pop("setup_sample_count")
            path.write_text(json.dumps(raw))
            self.assertEqual(load_measurements(path).profiles[0].timing_scope, "unspecified")


if __name__ == "__main__":
    unittest.main()
