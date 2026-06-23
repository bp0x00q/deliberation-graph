#!/usr/bin/env python3
"""Behavioral regression suite for deliberation-graph."""

from __future__ import annotations

import json
import multiprocessing
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
CLI = SCRIPT_DIR / "deliberation.py"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from graph_core import (  # noqa: E402
    RevisionConflict,
    ValidationError,
    add_branch,
    add_critique,
    add_edge,
    add_node,
    checkpoint,
    complete_first_pass,
    context_ledger_fragment,
    create_run,
    get_run_state,
    inspect_run,
    merge_branches,
    respond_to_critique,
    revise_branch,
    run_directory,
    score_branch,
    set_hard_gates,
    set_shortlist,
    sync_exports,
    synthesize,
    transition_phase,
    validate_run,
)


def _node_worker(project_text: str, run_id: str, worker: int, count: int) -> None:
    project = Path(project_text)
    for index in range(count):
        add_node(
            project,
            run_id=run_id,
            node_type="evidence",
            title=f"worker-{worker}-{index}",
            summary=f"Concurrent evidence {worker}/{index}",
            status="observed",
            source_refs=[f"fixture:{worker}:{index}"],
            tags=["contention"],
        )


class DeliberationGraphTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="deliberation-graph-test-")
        self.project = Path(self.temp.name).resolve()
        self.run_id = "test-run"

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def brief() -> Dict[str, Any]:
        return {
            "problem": "Choose a durable coordination architecture.",
            "context": "Two coding agents may work in one repository.",
            "hard_constraints": [
                "Do not depend on hidden chain-of-thought.",
                "Operate without a required network service.",
            ],
            "success_criteria": [
                "Preserve resumable operational state.",
                "Produce an auditable decision record.",
            ],
            "unknowns": ["Expected concurrent write rate."],
            "hard_gates": [
                {"id": "feasible", "label": "Technically feasible"},
                {"id": "constraints", "label": "Meets hard constraints"},
            ],
            "scoring_dimensions": [
                {"id": "correctness", "label": "Correctness", "weight": 0.6, "direction": "maximize"},
                {"id": "cost", "label": "Operational cost", "weight": 0.4, "direction": "minimize"},
            ],
        }

    def create(self, *, mode: str = "quick", run_id: str | None = None) -> Dict[str, Any]:
        return create_run(
            self.project,
            run_id=run_id or self.run_id,
            mode=mode,
            brief=self.brief(),
        )

    def move(self, target: str, *, run_id: str | None = None) -> Dict[str, Any]:
        rid = run_id or self.run_id
        state = get_run_state(self.project, rid)
        return transition_phase(
            self.project,
            run_id=rid,
            target_phase=target,
            expected_run_revision=state["revision"],
        )

    def add_three_branches(self, *, run_id: str | None = None) -> list[Dict[str, Any]]:
        rid = run_id or self.run_id
        rows = [
            ("SQLite event store", "A local SQLite runtime is available", "transactional consistency", "single-file database dependency"),
            ("Append-only JSON files", "Filesystem appends are sufficient", "deployment portability", "replay and indexing cost"),
            ("Shared MCP service", "Both agents can reach one service", "cross-agent coordination", "service availability dependency"),
        ]
        return [
            add_branch(
                self.project,
                run_id=rid,
                strategy=strategy,
                distinguishing_assumption=assumption,
                optimization_target=target,
                known_tradeoff=tradeoff,
            )
            for strategy, assumption, target, tradeoff in rows
        ]

    def to_evidence(self) -> list[Dict[str, Any]]:
        self.create()
        self.move("branching")
        self.add_three_branches()
        self.move("independent-exploration")
        branches = inspect_run(self.project, self.run_id)["branches"]
        for branch in branches:
            complete_first_pass(
                self.project,
                run_id=self.run_id,
                branch_id=branch["branch_id"],
                expected_revision=branch["revision"],
                summary=f"Independent analysis of {branch['strategy']}.",
                assumptions=[branch["distinguishing_assumption"]],
                risks=[branch["known_tradeoff"]],
                next_tests=["Run a representative contention fixture."],
            )
        self.move("evidence")
        return inspect_run(self.project, self.run_id)["branches"]

    def gate_and_score_all(self, *, scores: Mapping[str, float] | None = None) -> list[Dict[str, Any]]:
        score_values = dict(scores or {"correctness": 0.8, "cost": 0.3})
        branches = inspect_run(self.project, self.run_id)["branches"]
        for branch in branches:
            branch = set_hard_gates(
                self.project,
                run_id=self.run_id,
                branch_id=branch["branch_id"],
                expected_revision=branch["revision"],
                gates={
                    "feasible": {"passed": True, "evidence_ids": [], "reason": "Reviewed against the fixture."},
                    "constraints": {"passed": True, "evidence_ids": [], "reason": "No hard constraint violation found."},
                },
            )
            score_branch(
                self.project,
                run_id=self.run_id,
                branch_id=branch["branch_id"],
                expected_revision=branch["revision"],
                scores=score_values,
                rationale="Normalized fixture score.",
            )
        return inspect_run(self.project, self.run_id)["branches"]

    def to_revision(self) -> tuple[list[Dict[str, Any]], list[Dict[str, Any]]]:
        branches = self.to_evidence()
        self.gate_and_score_all()
        state = get_run_state(self.project, self.run_id)
        shortlist = [branches[0]["branch_id"], branches[1]["branch_id"]]
        set_shortlist(
            self.project,
            run_id=self.run_id,
            branch_ids=shortlist,
            expected_run_revision=state["revision"],
        )
        self.move("cross-critique")
        critiques = [
            add_critique(
                self.project,
                run_id=self.run_id,
                branch_id=shortlist[0],
                critic_branch_id=shortlist[1],
                attack_type="hidden-dependency",
                summary="The storage lock may be a hidden single-writer bottleneck.",
                severity="high",
            ),
            add_critique(
                self.project,
                run_id=self.run_id,
                branch_id=shortlist[1],
                critic_branch_id=shortlist[0],
                attack_type="scaling-failure",
                summary="Replay cost may grow with history length.",
                severity="high",
            ),
        ]
        self.move("revision")
        return inspect_run(self.project, self.run_id)["branches"], critiques

    def complete_run(self) -> Dict[str, Any]:
        branches, critiques = self.to_revision()
        for critique in critiques:
            respond_to_critique(
                self.project,
                run_id=self.run_id,
                critique_id=critique["critique_id"],
                expected_revision=critique["revision"],
                response="Mitigation added and validated against the fixture.",
                remaining_weakness="The fixture may not represent every workload.",
            )
        shortlist = get_run_state(self.project, self.run_id)["shortlist"]
        branch_map = {item["branch_id"]: item for item in inspect_run(self.project, self.run_id)["branches"]}
        for branch_id in shortlist:
            branch = branch_map[branch_id]
            revise_branch(
                self.project,
                run_id=self.run_id,
                branch_id=branch_id,
                expected_revision=branch["revision"],
                summary=f"Revised {branch['strategy']} with the critique mitigation.",
                changes=["Added a bounded contention/replay mitigation."],
                remaining_weakness="Performance still depends on workload shape.",
            )
        self.move("synthesis")
        state = get_run_state(self.project, self.run_id)
        all_branches = inspect_run(self.project, self.run_id)["branches"]
        selected, fallback = shortlist
        rejected = {
            item["branch_id"]: "Introduces a required shared service."
            for item in all_branches
            if item["branch_id"] not in {selected, fallback}
        }
        state = synthesize(
            self.project,
            run_id=self.run_id,
            expected_run_revision=state["revision"],
            selected_branch_id=selected,
            fallback_branch_id=fallback,
            rationale="The selected branch best meets consistency and deployment constraints.",
            residual_assumptions=["The local runtime includes SQLite."],
            reversal_evidence=["A representative fixture shows unacceptable writer contention."],
            rejected_reasons=rejected,
            surviving_caveat="The result is only as strong as the supplied evidence and branch diversity.",
        )
        return transition_phase(
            self.project,
            run_id=self.run_id,
            target_phase="complete",
            expected_run_revision=state["revision"],
        )

    def test_01_create_modes_and_normalize_weights(self) -> None:
        state = self.create(mode="deep")
        self.assertEqual(state["phase"], "framing")
        self.assertEqual(state["limits"]["minimum_branches"], 5)
        self.assertAlmostEqual(sum(item["weight"] for item in state["brief"]["scoring_dimensions"]), 1.0)
        self.assertTrue((run_directory(self.project, self.run_id) / "run.sqlite3").is_file())

    def test_02_duplicate_branch_axes_and_budget_are_rejected(self) -> None:
        self.create()
        self.move("branching")
        first = self.add_three_branches()[0]
        with self.assertRaises(ValidationError):
            add_branch(
                self.project,
                run_id=self.run_id,
                strategy=first["strategy"].upper(),
                distinguishing_assumption=first["distinguishing_assumption"],
                optimization_target=first["optimization_target"],
                known_tradeoff="Different wording does not matter.",
            )
        with self.assertRaises(ValidationError):
            add_branch(
                self.project,
                run_id=self.run_id,
                strategy="Fourth branch",
                distinguishing_assumption="A fourth assumption",
                optimization_target="something else",
                known_tradeoff="budget",
            )

    def test_03_phase_requires_minimum_distinct_branches(self) -> None:
        self.create()
        self.move("branching")
        add_branch(
            self.project,
            run_id=self.run_id,
            strategy="Only branch",
            distinguishing_assumption="Only assumption",
            optimization_target="only target",
            known_tradeoff="none",
        )
        with self.assertRaises(ValidationError):
            self.move("independent-exploration")

    def test_04_independent_first_pass_gate(self) -> None:
        self.create()
        self.move("branching")
        self.add_three_branches()
        self.move("independent-exploration")
        branch = inspect_run(self.project, self.run_id)["branches"][0]
        complete_first_pass(
            self.project,
            run_id=self.run_id,
            branch_id=branch["branch_id"],
            expected_revision=branch["revision"],
            summary="One completed branch.",
            assumptions=["Assumption"],
            risks=["Risk"],
            next_tests=[],
        )
        with self.assertRaises(ValidationError):
            self.move("evidence")

    def test_05_hidden_reasoning_fields_are_rejected(self) -> None:
        bad = self.brief()
        bad["chain_of_thought"] = "private scratchpad"
        with self.assertRaises(ValidationError):
            create_run(self.project, run_id=self.run_id, mode="quick", brief=bad)
        self.create()
        self.move("branching")
        with self.assertRaises(ValidationError):
            add_node(
                self.project,
                run_id=self.run_id,
                node_type="evidence",
                title="Bad",
                summary="Bad payload",
                status="unverified",
                data={"internal_monologue": "not accepted"},
            )

    def test_06_observed_evidence_requires_source_reference(self) -> None:
        self.create()
        with self.assertRaises(ValidationError):
            add_node(
                self.project,
                run_id=self.run_id,
                node_type="evidence",
                title="Observation",
                summary="The fixture passed.",
                status="observed",
            )
        node = add_node(
            self.project,
            run_id=self.run_id,
            node_type="evidence",
            title="Observation",
            summary="The fixture passed.",
            status="observed",
            source_refs=["test:fixture-1"],
        )
        self.assertEqual(node["status"], "observed")

    def test_07_graph_integrity_and_dependency_cycle(self) -> None:
        self.create()
        a = add_node(self.project, run_id=self.run_id, node_type="question", title="A", summary="Question A")
        b = add_node(self.project, run_id=self.run_id, node_type="hypothesis", title="B", summary="Hypothesis B")
        add_edge(self.project, run_id=self.run_id, source_id=a["node_id"], target_id=b["node_id"], relation="depends_on")
        with self.assertRaises(ValidationError):
            add_edge(self.project, run_id=self.run_id, source_id=b["node_id"], target_id=a["node_id"], relation="depends_on")
        with self.assertRaises(ValidationError):
            add_edge(self.project, run_id=self.run_id, source_id=a["node_id"], target_id="node-999999", relation="supports")
        self.assertTrue(validate_run(self.project, self.run_id)["ok"])

    def test_08_hard_gates_are_required_before_scoring(self) -> None:
        branches = self.to_evidence()
        branch = branches[0]
        with self.assertRaises(ValidationError):
            score_branch(
                self.project,
                run_id=self.run_id,
                branch_id=branch["branch_id"],
                expected_revision=branch["revision"],
                scores={"correctness": 0.8, "cost": 0.2},
                rationale="Premature score.",
            )
        branch = set_hard_gates(
            self.project,
            run_id=self.run_id,
            branch_id=branch["branch_id"],
            expected_revision=branch["revision"],
            gates={
                "feasible": {"passed": False, "evidence_ids": [], "reason": "Not feasible."},
                "constraints": {"passed": True, "evidence_ids": [], "reason": "Constraints met."},
            },
        )
        with self.assertRaises(ValidationError):
            score_branch(
                self.project,
                run_id=self.run_id,
                branch_id=branch["branch_id"],
                expected_revision=branch["revision"],
                scores={"correctness": 0.8, "cost": 0.2},
                rationale="Failed gate.",
            )

    def test_09_scoring_is_deterministic_and_honors_minimize_direction(self) -> None:
        branches = self.to_evidence()
        branch = set_hard_gates(
            self.project,
            run_id=self.run_id,
            branch_id=branches[0]["branch_id"],
            expected_revision=branches[0]["revision"],
            gates={
                "feasible": {"passed": True, "evidence_ids": [], "reason": "yes"},
                "constraints": {"passed": True, "evidence_ids": [], "reason": "yes"},
            },
        )
        branch = score_branch(
            self.project,
            run_id=self.run_id,
            branch_id=branch["branch_id"],
            expected_revision=branch["revision"],
            scores={"correctness": 0.9, "cost": 0.2},
            rationale="fixture",
        )
        self.assertAlmostEqual(branch["weighted_score"], 0.86)

    def test_10_shortlist_requires_evidence_qualified_scored_branches(self) -> None:
        branches = self.to_evidence()
        state = get_run_state(self.project, self.run_id)
        with self.assertRaises(ValidationError):
            set_shortlist(
                self.project,
                run_id=self.run_id,
                branch_ids=[branches[0]["branch_id"], branches[1]["branch_id"]],
                expected_run_revision=state["revision"],
            )
        self.gate_and_score_all()
        state = get_run_state(self.project, self.run_id)
        state = set_shortlist(
            self.project,
            run_id=self.run_id,
            branch_ids=[branches[0]["branch_id"], branches[1]["branch_id"]],
            expected_run_revision=state["revision"],
        )
        self.assertEqual(len(state["shortlist"]), 2)

    def test_11_critique_requires_cross_phase_and_independent_attacker(self) -> None:
        branches = self.to_evidence()
        self.gate_and_score_all()
        state = get_run_state(self.project, self.run_id)
        ids = [branches[0]["branch_id"], branches[1]["branch_id"]]
        set_shortlist(self.project, run_id=self.run_id, branch_ids=ids, expected_run_revision=state["revision"])
        with self.assertRaises(ValidationError):
            add_critique(
                self.project,
                run_id=self.run_id,
                branch_id=ids[0],
                critic_branch_id=ids[1],
                attack_type="counterexample",
                summary="Too early.",
                severity="medium",
            )
        self.move("cross-critique")
        with self.assertRaises(ValidationError):
            add_critique(
                self.project,
                run_id=self.run_id,
                branch_id=ids[0],
                critic_branch_id=ids[0],
                attack_type="counterexample",
                summary="Self critique.",
                severity="medium",
            )

    def test_12_revision_requires_resolved_critique(self) -> None:
        branches, critiques = self.to_revision()
        branch_id = get_run_state(self.project, self.run_id)["shortlist"][0]
        branch = {item["branch_id"]: item for item in branches}[branch_id]
        with self.assertRaises(ValidationError):
            revise_branch(
                self.project,
                run_id=self.run_id,
                branch_id=branch_id,
                expected_revision=branch["revision"],
                summary="Premature revision",
                changes=["None"],
                remaining_weakness="Unresolved critique",
            )
        critique = next(item for item in critiques if item["branch_id"] == branch_id)
        respond_to_critique(
            self.project,
            run_id=self.run_id,
            critique_id=critique["critique_id"],
            expected_revision=critique["revision"],
            response="Mitigated.",
            remaining_weakness="Some risk remains.",
        )
        branch = {item["branch_id"]: item for item in inspect_run(self.project, self.run_id)["branches"]}[branch_id]
        revised = revise_branch(
            self.project,
            run_id=self.run_id,
            branch_id=branch_id,
            expected_revision=branch["revision"],
            summary="Revised branch",
            changes=["Added mitigation"],
            remaining_weakness="Residual risk",
        )
        self.assertEqual(revised["status"], "revised")

    def test_13_merge_creates_convergent_branch_and_edges(self) -> None:
        create_run(
            self.project,
            run_id=self.run_id,
            mode="quick",
            brief=self.brief(),
            branch_budget=4,
            minimum_branches=3,
        )
        self.move("branching")
        self.add_three_branches()
        self.move("independent-exploration")
        for branch in inspect_run(self.project, self.run_id)["branches"]:
            complete_first_pass(
                self.project,
                run_id=self.run_id,
                branch_id=branch["branch_id"],
                expected_revision=branch["revision"],
                summary=f"Independent analysis of {branch['strategy']}.",
                assumptions=[branch["distinguishing_assumption"]],
                risks=[branch["known_tradeoff"]],
                next_tests=["Compare the hybrid against both parents."],
            )
        self.move("evidence")
        branches = inspect_run(self.project, self.run_id)["branches"]
        merged = merge_branches(
            self.project,
            run_id=self.run_id,
            parent_branch_ids=[branches[0]["branch_id"], branches[1]["branch_id"]],
            strategy="SQLite authority with JSON audit export",
            distinguishing_assumption="Local transactions and portable exports are both valuable",
            optimization_target="consistency plus inspectability",
            known_tradeoff="two representations must be reconciled",
        )
        exported = inspect_run(self.project, self.run_id)
        merge_edges = [item for item in exported["edges"] if item["relation"] == "merges_with"]
        self.assertEqual(merged["parent_branch_ids"], [branches[0]["branch_id"], branches[1]["branch_id"]])
        self.assertEqual(len(merge_edges), 2)

    def test_14_synthesis_requires_fallback_reversal_and_complete_rejections(self) -> None:
        branches, critiques = self.to_revision()
        for critique in critiques:
            respond_to_critique(
                self.project,
                run_id=self.run_id,
                critique_id=critique["critique_id"],
                expected_revision=critique["revision"],
                response="Mitigated.",
                remaining_weakness="Residual.",
            )
        shortlist = get_run_state(self.project, self.run_id)["shortlist"]
        branch_map = {item["branch_id"]: item for item in inspect_run(self.project, self.run_id)["branches"]}
        for branch_id in shortlist:
            branch = branch_map[branch_id]
            revise_branch(
                self.project,
                run_id=self.run_id,
                branch_id=branch_id,
                expected_revision=branch["revision"],
                summary="Revised",
                changes=["Mitigation"],
                remaining_weakness="Residual",
            )
        self.move("synthesis")
        state = get_run_state(self.project, self.run_id)
        with self.assertRaises(ValidationError):
            synthesize(
                self.project,
                run_id=self.run_id,
                expected_run_revision=state["revision"],
                selected_branch_id=shortlist[0],
                fallback_branch_id=shortlist[1],
                rationale="Missing rejection coverage.",
                residual_assumptions=["Assumption"],
                reversal_evidence=["Evidence"],
                rejected_reasons={},
                surviving_caveat="Caveat",
            )

    def test_15_complete_run_validates_and_preserves_caveat(self) -> None:
        state = self.complete_run()
        self.assertEqual(state["phase"], "complete")
        self.assertIn("only as strong", state["synthesis"]["surviving_caveat"])
        report = validate_run(self.project, self.run_id)
        self.assertTrue(report["ok"], report)

    def test_16_context_ledger_fragment_is_nonexecuting_and_category_safe(self) -> None:
        self.create()
        fragment = context_ledger_fragment(
            self.project,
            run_id=self.run_id,
            source_event_id="evt-000000000042",
        )
        self.assertEqual(fragment["operations"][0]["op"], "set_active_deliberation")
        self.assertNotIn("source_dispositions", fragment)
        self.assertIn("does not execute", fragment["integration_note"])

    def test_17_cursor_checkpoint_survives_export(self) -> None:
        state = self.create()
        state = checkpoint(
            self.project,
            run_id=self.run_id,
            expected_run_revision=state["revision"],
            last_completed="Compared storage APIs",
            in_progress="Writing contention fixtures",
            next_action="Run the multiprocess fixture",
            blockers=["Need Windows results"],
        )
        exported = json.loads((run_directory(self.project, self.run_id) / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(exported["state"]["cursor"], state["cursor"])

    def test_18_concurrent_node_updates_preserve_count_and_exports(self) -> None:
        self.create()
        workers = 4
        count = 12
        ctx = multiprocessing.get_context("spawn")
        processes = [
            ctx.Process(target=_node_worker, args=(str(self.project), self.run_id, worker, count))
            for worker in range(workers)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(30)
            self.assertEqual(process.exitcode, 0)
        result = inspect_run(self.project, self.run_id)
        evidence = [item for item in result["nodes"] if item["node_type"] == "evidence"]
        self.assertEqual(len(evidence), workers * count)
        self.assertEqual(len({item["node_id"] for item in evidence}), workers * count)
        self.assertTrue(validate_run(self.project, self.run_id)["ok"])

    def test_19_branch_revision_conflict_rejects_stale_update(self) -> None:
        self.create()
        self.move("branching")
        self.add_three_branches()
        self.move("independent-exploration")
        branch = inspect_run(self.project, self.run_id)["branches"][0]
        complete_first_pass(
            self.project,
            run_id=self.run_id,
            branch_id=branch["branch_id"],
            expected_revision=branch["revision"],
            summary="First",
            assumptions=["A"],
            risks=["R"],
            next_tests=[],
        )
        with self.assertRaises(RevisionConflict):
            complete_first_pass(
                self.project,
                run_id=self.run_id,
                branch_id=branch["branch_id"],
                expected_revision=branch["revision"],
                summary="Stale",
                assumptions=["A"],
                risks=["R"],
                next_tests=[],
            )

    def test_20_report_rendering_is_deterministic(self) -> None:
        self.create()
        first = (run_directory(self.project, self.run_id) / "report.md").read_bytes()
        sync_exports(self.project, self.run_id)
        second = (run_directory(self.project, self.run_id) / "report.md").read_bytes()
        self.assertEqual(first, second)
        self.assertIn(b"does not expose or claim to preserve hidden chain-of-thought", second)

    def test_21_schemas_are_valid_json(self) -> None:
        names = {path.name for path in (SKILL_ROOT / "schemas").glob("*.schema.json")}
        self.assertEqual(
            names,
            {
                "brief.schema.json",
                "branch.schema.json",
                "graph.schema.json",
                "run.schema.json",
                "synthesis.schema.json",
            },
        )
        for path in (SKILL_ROOT / "schemas").glob("*.schema.json"):
            schema = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")

    def test_22_cli_malformed_input_fails_cleanly(self) -> None:
        bad = self.project / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--project",
                str(self.project),
                "create",
                "--run-id",
                self.run_id,
                "--mode",
                "quick",
                "--brief-file",
                str(bad),
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot read JSON", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_23_manifest_matches_package(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "write_manifest.py"), "--check"],
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("RESULT: PASS", result.stdout)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(DeliberationGraphTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(f"\nRESULT: {'PASS' if result.wasSuccessful() else 'FAIL'}")
    print(f"Tests: {result.testsRun}; failures: {len(result.failures)}; errors: {len(result.errors)}")
    raise SystemExit(0 if result.wasSuccessful() else 1)
