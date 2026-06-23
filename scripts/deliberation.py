#!/usr/bin/env python3
"""Command-line interface for deliberation-graph."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from graph_core import (  # noqa: E402
    DeliberationError,
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
    score_branch,
    set_hard_gates,
    set_shortlist,
    sync_exports,
    synthesize,
    transition_phase,
    validate_run,
)


def _json_dump(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _read_json(path: str) -> Any:
    try:
        if path == "-":
            return json.load(sys.stdin)
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read JSON from {path}: {exc}") from exc


def _read_json_object(path: str, label: str) -> Dict[str, Any]:
    value = _read_json(path)
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must be a JSON object")
    return value


def _read_string_list(path: str, label: str) -> List[str]:
    value = _read_json(path)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValidationError(f"{label} must be a JSON array of strings")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage explicit multi-branch deliberation state without model API calls or hidden reasoning traces."
    )
    parser.add_argument("--project", default=".", help="project root (default: current directory)")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="create a deliberation run from a brief JSON file")
    create.add_argument("--run-id", required=True)
    create.add_argument("--mode", choices=["quick", "deep", "exhaustive"], default="deep")
    create.add_argument("--brief-file", required=True)
    create.add_argument("--branch-budget", type=int)
    create.add_argument("--minimum-branches", type=int)

    phase = sub.add_parser("phase", help="advance exactly one deliberation phase")
    phase.add_argument("--run-id", required=True)
    phase.add_argument("--target", required=True)
    phase.add_argument("--expected-run-revision", required=True, type=int)

    branch = sub.add_parser("add-branch", help="add a branch with explicit distinguishing axes")
    branch.add_argument("--run-id", required=True)
    branch.add_argument("--strategy", required=True)
    branch.add_argument("--distinguishing-assumption", required=True)
    branch.add_argument("--optimization-target", required=True)
    branch.add_argument("--known-tradeoff", required=True)
    branch.add_argument("--introduced-by", default="agent")

    first = sub.add_parser("first-pass", help="complete one branch's independent first pass")
    first.add_argument("--run-id", required=True)
    first.add_argument("--branch-id", required=True)
    first.add_argument("--expected-branch-revision", required=True, type=int)
    first.add_argument("--summary", required=True)
    first.add_argument("--assumption", action="append", default=[])
    first.add_argument("--risk", action="append", default=[])
    first.add_argument("--next-test", action="append", default=[])

    node = sub.add_parser("add-node", help="add explicit evidence, assumption, experiment, risk, or other graph node")
    node.add_argument("--run-id", required=True)
    node.add_argument("--type", required=True)
    node.add_argument("--title", required=True)
    node.add_argument("--summary", required=True)
    node.add_argument("--branch-id")
    node.add_argument("--status", default="active")
    node.add_argument("--source-ref", action="append", default=[])
    node.add_argument("--tag", action="append", default=[])
    node.add_argument("--data-file")

    edge = sub.add_parser("connect", help="connect two graph nodes")
    edge.add_argument("--run-id", required=True)
    edge.add_argument("--source", required=True)
    edge.add_argument("--target", required=True)
    edge.add_argument("--relation", required=True)
    edge.add_argument("--rationale", default="")

    gates = sub.add_parser("gates", help="evaluate all configured hard gates for a branch")
    gates.add_argument("--run-id", required=True)
    gates.add_argument("--branch-id", required=True)
    gates.add_argument("--expected-branch-revision", required=True, type=int)
    gates.add_argument("--gates-file", required=True)

    score = sub.add_parser("score", help="score a branch after every hard gate passes")
    score.add_argument("--run-id", required=True)
    score.add_argument("--branch-id", required=True)
    score.add_argument("--expected-branch-revision", required=True, type=int)
    score.add_argument("--scores-file", required=True)
    score.add_argument("--rationale", required=True)

    shortlist = sub.add_parser("shortlist", help="select evidence-qualified branches for cross-critique")
    shortlist.add_argument("--run-id", required=True)
    shortlist.add_argument("--branch-id", action="append", required=True)
    shortlist.add_argument("--expected-run-revision", required=True, type=int)

    critique = sub.add_parser("critique", help="attack a shortlisted branch")
    critique.add_argument("--run-id", required=True)
    critique.add_argument("--branch-id", required=True)
    critique.add_argument("--critic-branch-id", required=True)
    critique.add_argument("--attack-type", required=True)
    critique.add_argument("--summary", required=True)
    critique.add_argument("--severity", choices=["low", "medium", "high", "blocking"], required=True)
    critique.add_argument("--source-ref", action="append", default=[])

    respond = sub.add_parser("respond", help="answer or accept a critique")
    respond.add_argument("--run-id", required=True)
    respond.add_argument("--critique-id", required=True)
    respond.add_argument("--expected-critique-revision", required=True, type=int)
    respond.add_argument("--response", required=True)
    respond.add_argument("--remaining-weakness", default="")
    respond.add_argument("--accept", action="store_true")

    revise = sub.add_parser("revise", help="revise a shortlisted branch after its critiques are resolved")
    revise.add_argument("--run-id", required=True)
    revise.add_argument("--branch-id", required=True)
    revise.add_argument("--expected-branch-revision", required=True, type=int)
    revise.add_argument("--summary", required=True)
    revise.add_argument("--change", action="append", required=True)
    revise.add_argument("--remaining-weakness", required=True)
    revise.add_argument("--assumption", action="append")
    revise.add_argument("--risk", action="append")

    merge = sub.add_parser("merge", help="create a convergent branch from two or more parents")
    merge.add_argument("--run-id", required=True)
    merge.add_argument("--parent-branch-id", action="append", required=True)
    merge.add_argument("--strategy", required=True)
    merge.add_argument("--distinguishing-assumption", required=True)
    merge.add_argument("--optimization-target", required=True)
    merge.add_argument("--known-tradeoff", required=True)

    cursor = sub.add_parser("checkpoint", help="record the literal next deliberation action")
    cursor.add_argument("--run-id", required=True)
    cursor.add_argument("--expected-run-revision", required=True, type=int)
    cursor.add_argument("--last-completed", required=True)
    cursor.add_argument("--in-progress", required=True)
    cursor.add_argument("--next-action", required=True)
    cursor.add_argument("--blocker", action="append", default=[])

    synthesis = sub.add_parser("synthesize", help="record selected/fallback branches and reversal criteria")
    synthesis.add_argument("--run-id", required=True)
    synthesis.add_argument("--expected-run-revision", required=True, type=int)
    synthesis.add_argument("--selected-branch-id", required=True)
    synthesis.add_argument("--fallback-branch-id", required=True)
    synthesis.add_argument("--rationale", required=True)
    synthesis.add_argument("--residual-assumption", action="append", required=True)
    synthesis.add_argument("--reversal-evidence", action="append", required=True)
    synthesis.add_argument("--rejected-reasons-file", required=True)
    synthesis.add_argument("--surviving-caveat", required=True)

    inspect = sub.add_parser("inspect", help="print the complete exported run state")
    inspect.add_argument("--run-id", required=True)

    validate = sub.add_parser("validate", help="validate SQLite state, graph integrity, and exports")
    validate.add_argument("--run-id", required=True)
    validate.add_argument("--json", action="store_true")

    report = sub.add_parser("report", help="regenerate deterministic JSON and Markdown exports")
    report.add_argument("--run-id", required=True)

    fragment = sub.add_parser("context-ledger-fragment", help="emit a context-ledger operation fragment without executing it")
    fragment.add_argument("--run-id", required=True)
    fragment.add_argument("--source-event-id", required=True)
    fragment.add_argument("--clear", action="store_true")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    project = Path(args.project).expanduser().resolve()
    try:
        if args.command == "create":
            state = create_run(
                project,
                run_id=args.run_id,
                mode=args.mode,
                brief=_read_json_object(args.brief_file, "brief"),
                branch_budget=args.branch_budget,
                minimum_branches=args.minimum_branches,
            )
            _json_dump({"ok": True, "state": state})
            return 0

        if args.command == "phase":
            state = transition_phase(
                project,
                run_id=args.run_id,
                target_phase=args.target,
                expected_run_revision=args.expected_run_revision,
            )
            _json_dump({"ok": True, "state": state})
            return 0

        if args.command == "add-branch":
            result = add_branch(
                project,
                run_id=args.run_id,
                strategy=args.strategy,
                distinguishing_assumption=args.distinguishing_assumption,
                optimization_target=args.optimization_target,
                known_tradeoff=args.known_tradeoff,
                introduced_by=args.introduced_by,
            )
            _json_dump({"ok": True, "branch": result})
            return 0

        if args.command == "first-pass":
            result = complete_first_pass(
                project,
                run_id=args.run_id,
                branch_id=args.branch_id,
                expected_revision=args.expected_branch_revision,
                summary=args.summary,
                assumptions=args.assumption,
                risks=args.risk,
                next_tests=args.next_test,
            )
            _json_dump({"ok": True, "branch": result})
            return 0

        if args.command == "add-node":
            data = _read_json_object(args.data_file, "node data") if args.data_file else None
            result = add_node(
                project,
                run_id=args.run_id,
                node_type=args.type,
                title=args.title,
                summary=args.summary,
                branch_id=args.branch_id,
                status=args.status,
                source_refs=args.source_ref,
                tags=args.tag,
                data=data,
            )
            _json_dump({"ok": True, "node": result})
            return 0

        if args.command == "connect":
            result = add_edge(
                project,
                run_id=args.run_id,
                source_id=args.source,
                target_id=args.target,
                relation=args.relation,
                rationale=args.rationale,
            )
            _json_dump({"ok": True, "edge": result})
            return 0

        if args.command == "gates":
            result = set_hard_gates(
                project,
                run_id=args.run_id,
                branch_id=args.branch_id,
                expected_revision=args.expected_branch_revision,
                gates=_read_json_object(args.gates_file, "hard gates"),
            )
            _json_dump({"ok": True, "branch": result})
            return 0

        if args.command == "score":
            scores = _read_json_object(args.scores_file, "scores")
            result = score_branch(
                project,
                run_id=args.run_id,
                branch_id=args.branch_id,
                expected_revision=args.expected_branch_revision,
                scores=scores,
                rationale=args.rationale,
            )
            _json_dump({"ok": True, "branch": result})
            return 0

        if args.command == "shortlist":
            state = set_shortlist(
                project,
                run_id=args.run_id,
                branch_ids=args.branch_id,
                expected_run_revision=args.expected_run_revision,
            )
            _json_dump({"ok": True, "state": state})
            return 0

        if args.command == "critique":
            result = add_critique(
                project,
                run_id=args.run_id,
                branch_id=args.branch_id,
                critic_branch_id=args.critic_branch_id,
                attack_type=args.attack_type,
                summary=args.summary,
                severity=args.severity,
                source_refs=args.source_ref,
            )
            _json_dump({"ok": True, "critique": result})
            return 0

        if args.command == "respond":
            result = respond_to_critique(
                project,
                run_id=args.run_id,
                critique_id=args.critique_id,
                expected_revision=args.expected_critique_revision,
                response=args.response,
                remaining_weakness=args.remaining_weakness,
                accept=args.accept,
            )
            _json_dump({"ok": True, "critique": result})
            return 0

        if args.command == "revise":
            result = revise_branch(
                project,
                run_id=args.run_id,
                branch_id=args.branch_id,
                expected_revision=args.expected_branch_revision,
                summary=args.summary,
                changes=args.change,
                remaining_weakness=args.remaining_weakness,
                assumptions=args.assumption,
                risks=args.risk,
            )
            _json_dump({"ok": True, "branch": result})
            return 0

        if args.command == "merge":
            result = merge_branches(
                project,
                run_id=args.run_id,
                parent_branch_ids=args.parent_branch_id,
                strategy=args.strategy,
                distinguishing_assumption=args.distinguishing_assumption,
                optimization_target=args.optimization_target,
                known_tradeoff=args.known_tradeoff,
            )
            _json_dump({"ok": True, "branch": result})
            return 0

        if args.command == "checkpoint":
            state = checkpoint(
                project,
                run_id=args.run_id,
                expected_run_revision=args.expected_run_revision,
                last_completed=args.last_completed,
                in_progress=args.in_progress,
                next_action=args.next_action,
                blockers=args.blocker,
            )
            _json_dump({"ok": True, "state": state})
            return 0

        if args.command == "synthesize":
            state = synthesize(
                project,
                run_id=args.run_id,
                expected_run_revision=args.expected_run_revision,
                selected_branch_id=args.selected_branch_id,
                fallback_branch_id=args.fallback_branch_id,
                rationale=args.rationale,
                residual_assumptions=args.residual_assumption,
                reversal_evidence=args.reversal_evidence,
                rejected_reasons=_read_json_object(args.rejected_reasons_file, "rejected reasons"),
                surviving_caveat=args.surviving_caveat,
            )
            _json_dump({"ok": True, "state": state})
            return 0

        if args.command == "inspect":
            _json_dump(inspect_run(project, args.run_id))
            return 0

        if args.command == "validate":
            report = validate_run(project, args.run_id)
            if args.json:
                _json_dump(report)
            else:
                print("RESULT:", "PASS" if report["ok"] else "FAIL")
                for error in report["errors"]:
                    print("ERROR:", error)
                for warning in report["warnings"]:
                    print("WARNING:", warning)
                for key, value in sorted(report["checks"].items()):
                    print(f"{key}: {value}")
            return 0 if report["ok"] else 1

        if args.command == "report":
            run_path, graph_path, report_path = sync_exports(project, args.run_id)
            _json_dump(
                {
                    "ok": True,
                    "run": str(run_path),
                    "graph": str(graph_path),
                    "report": str(report_path),
                }
            )
            return 0

        if args.command == "context-ledger-fragment":
            _json_dump(
                context_ledger_fragment(
                    project,
                    run_id=args.run_id,
                    source_event_id=args.source_event_id,
                    clear=args.clear,
                )
            )
            return 0

        raise ValidationError(f"unhandled command: {args.command}")
    except DeliberationError as exc:
        print(f"deliberation-graph: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("deliberation-graph: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
