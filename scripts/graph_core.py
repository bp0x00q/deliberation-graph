#!/usr/bin/env python3
"""Deterministic state manager for the deliberation-graph Agent Skill.

The host agent or its subagents perform analysis. This module stores only
externally stated decision records: branches, assumptions, evidence, critiques,
experiments, scores, decisions, and the literal execution cursor. It neither
calls a model API nor asks for hidden chain-of-thought.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, MutableMapping, Optional, Sequence, Tuple

SCHEMA_VERSION = 1
RUNS_DIR = Path(".deliberation") / "runs"
DB_FILE = "run.sqlite3"
RUN_EXPORT = "run.json"
GRAPH_EXPORT = "graph.json"
REPORT_FILE = "report.md"

RUN_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$")
BRANCH_ID_RE = re.compile(r"^branch-[0-9]{3,6}$")
NODE_ID_RE = re.compile(r"^node-[0-9]{6}$")
CRITIQUE_ID_RE = re.compile(r"^critique-[0-9]{6}$")
EDGE_ID_RE = re.compile(r"^edge-[0-9]{6}$")

MODE_DEFAULTS: Dict[str, Dict[str, int]] = {
    "quick": {"minimum_branches": 3, "branch_budget": 3, "shortlist_size": 2},
    "deep": {"minimum_branches": 5, "branch_budget": 7, "shortlist_size": 3},
    "exhaustive": {"minimum_branches": 5, "branch_budget": 12, "shortlist_size": 4},
}

PHASES = [
    "framing",
    "branching",
    "independent-exploration",
    "evidence",
    "cross-critique",
    "revision",
    "synthesis",
    "complete",
]

BRANCH_STATUSES = {
    "proposed",
    "exploring",
    "first-pass-complete",
    "shortlisted",
    "revised",
    "selected",
    "fallback",
    "rejected",
    "deferred",
    "merged",
}

NODE_TYPES = {
    "problem",
    "constraint",
    "question",
    "hypothesis",
    "approach",
    "evidence",
    "assumption",
    "experiment",
    "critique",
    "risk",
    "decision",
    "synthesis",
}

EDGE_TYPES = {
    "supports",
    "contradicts",
    "depends_on",
    "supersedes",
    "derived_from",
    "tests",
    "blocks",
    "merges_with",
    "selected_over",
}

ATTACK_TYPES = {
    "constraint-violation",
    "hidden-dependency",
    "incorrect-assumption",
    "operational-failure",
    "security-failure",
    "scaling-failure",
    "migration-failure",
    "counterexample",
    "simpler-alternative",
    "evidence-quality",
}

CRITIQUE_STATUSES = {"open", "answered", "accepted"}
EVIDENCE_STATUSES = {"observed", "unverified", "disputed", "superseded"}
DEPENDENCY_RELATIONS = {"depends_on", "derived_from"}

FORBIDDEN_KEYS = {
    "chain_of_thought",
    "chain-of-thought",
    "cot",
    "reasoning_trace",
    "private_reasoning",
    "internal_monologue",
    "hidden_reasoning",
    "scratchpad",
    "raw_thoughts",
}


class DeliberationError(RuntimeError):
    """Base error for deterministic deliberation failures."""


class ValidationError(DeliberationError):
    """Raised when supplied data violates the run contract."""


class RevisionConflict(DeliberationError):
    """Raised when an update targets a stale branch or run revision."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_text(value: str) -> str:
    return " ".join(value.lower().split())


def _require_string(data: Mapping[str, Any], key: str, *, allow_empty: bool = False) -> str:
    value = data.get(key)
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValidationError(f"{key} must be a {'string' if allow_empty else 'non-empty string'}")
    return value.strip() if not allow_empty else value


def _string_list(value: Any, label: str, *, allow_empty: bool = True) -> List[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValidationError(f"{label} must be an array of non-empty strings")
    if not allow_empty and not value:
        raise ValidationError(f"{label} must not be empty")
    if len(set(value)) != len(value):
        raise ValidationError(f"{label} must not contain duplicates")
    return [item.strip() for item in value]


def _reject_forbidden_keys(value: Any, path: str = "input") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key).strip().lower().replace(" ", "_")
            if key_text in FORBIDDEN_KEYS:
                raise ValidationError(
                    f"{path}.{key} is not accepted; record decision-relevant summaries, assumptions, evidence, and critiques instead"
                )
            _reject_forbidden_keys(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_forbidden_keys(child, f"{path}[{index}]")


def safe_run_id(value: str) -> str:
    if not isinstance(value, str) or not RUN_ID_RE.fullmatch(value):
        raise ValidationError("run_id must be 1-80 lowercase letters, digits, or hyphens and cannot start/end with a hyphen")
    return value


def project_root(path: Path | str) -> Path:
    return Path(path).expanduser().resolve()


def run_directory(project: Path | str, run_id: str) -> Path:
    root = project_root(project)
    safe = safe_run_id(run_id)
    path = (root / RUNS_DIR / safe).resolve()
    expected_parent = (root / RUNS_DIR).resolve()
    try:
        path.relative_to(expected_parent)
    except ValueError as exc:
        raise ValidationError("run directory escapes project root") from exc
    return path


def _chmod_private(path: Path, *, directory: bool = False) -> None:
    try:
        path.chmod(0o700 if directory else 0o600)
    except OSError:
        pass


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_private(path.parent, directory=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _chmod_private(temp)
        os.replace(temp, path)
        _fsync_directory(path.parent)
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


@contextmanager
def _export_lock(directory: Path, timeout: float = 15.0, stale_after: float = 300.0) -> Iterator[None]:
    lock_path = directory / ".export.lock"
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(fd, canonical_json({"pid": os.getpid(), "created_at": utc_now()}).encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > stale_after:
                    lock_path.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise ValidationError(f"timed out acquiring export lock: {lock_path}")
            time.sleep(0.03)
    try:
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _connect(directory: Path, timeout_ms: int = 15000) -> sqlite3.Connection:
    directory.mkdir(parents=True, exist_ok=True)
    _chmod_private(directory, directory=True)
    conn = sqlite3.connect(
        directory / DB_FILE,
        timeout=max(timeout_ms, 100) / 1000,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {int(timeout_ms)}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS run_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            revision INTEGER NOT NULL,
            document TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS branches (
            branch_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            revision INTEGER NOT NULL,
            signature TEXT NOT NULL UNIQUE,
            document TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS nodes (
            node_id TEXT PRIMARY KEY,
            node_type TEXT NOT NULL,
            branch_id TEXT,
            revision INTEGER NOT NULL,
            document TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (branch_id) REFERENCES branches(branch_id) ON DELETE RESTRICT
        );
        CREATE TABLE IF NOT EXISTS edges (
            edge_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            relation TEXT NOT NULL,
            document TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(source_id, target_id, relation),
            FOREIGN KEY (source_id) REFERENCES nodes(node_id) ON DELETE RESTRICT,
            FOREIGN KEY (target_id) REFERENCES nodes(node_id) ON DELETE RESTRICT
        );
        CREATE TABLE IF NOT EXISTS critiques (
            critique_id TEXT PRIMARY KEY,
            branch_id TEXT NOT NULL,
            critic_branch_id TEXT,
            status TEXT NOT NULL,
            revision INTEGER NOT NULL,
            document TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (branch_id) REFERENCES branches(branch_id) ON DELETE RESTRICT
        );
        CREATE INDEX IF NOT EXISTS idx_nodes_branch ON nodes(branch_id);
        CREATE INDEX IF NOT EXISTS idx_critiques_branch ON critiques(branch_id);
        CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
        CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);
        """
    )
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    for key in ("branch_counter", "node_counter", "edge_counter", "critique_counter"):
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES (?, '0')", (key,))


@contextmanager
def _transaction(conn: sqlite3.Connection, retries: int = 8) -> Iterator[None]:
    attempt = 0
    while True:
        try:
            conn.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt >= retries:
                raise
            time.sleep(min(0.02 * (2**attempt), 0.5))
            attempt += 1
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _next_id(conn: sqlite3.Connection, counter: str, prefix: str, width: int = 6) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (counter,)).fetchone()
    current = int(row["value"]) if row else 0
    value = current + 1
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (counter, str(value)),
    )
    return f"{prefix}-{value:0{width}d}"


def _load_json_document(row: sqlite3.Row, field: str = "document") -> Dict[str, Any]:
    try:
        value = json.loads(row[field])
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValidationError("stored deliberation JSON is corrupt") from exc
    if not isinstance(value, dict):
        raise ValidationError("stored deliberation document must be an object")
    return value


def _load_run_state(conn: sqlite3.Connection) -> Dict[str, Any]:
    row = conn.execute("SELECT document FROM run_state WHERE singleton = 1").fetchone()
    if row is None:
        raise ValidationError("run state is missing")
    return _load_json_document(row)


def _put_run_state(conn: sqlite3.Connection, state: Mapping[str, Any]) -> None:
    conn.execute(
        "UPDATE run_state SET revision = ?, document = ? WHERE singleton = 1",
        (int(state["revision"]), canonical_json(state)),
    )


def _bump_run(state: MutableMapping[str, Any]) -> None:
    state["revision"] = int(state.get("revision", 0)) + 1
    state["updated_at"] = utc_now()


def _branch_signature(strategy: str, distinguishing_assumption: str, optimization_target: str) -> str:
    return sha256_text(
        canonical_json(
            [
                _normalize_text(strategy),
                _normalize_text(distinguishing_assumption),
                _normalize_text(optimization_target),
            ]
        )
    )


def _validate_brief(brief: Mapping[str, Any]) -> Dict[str, Any]:
    _reject_forbidden_keys(brief, "brief")
    normalized: Dict[str, Any] = {
        "problem": _require_string(brief, "problem"),
        "context": str(brief.get("context", "")),
        "hard_constraints": _string_list(brief.get("hard_constraints", []), "hard_constraints", allow_empty=False),
        "success_criteria": _string_list(brief.get("success_criteria", []), "success_criteria", allow_empty=False),
        "unknowns": _string_list(brief.get("unknowns", []), "unknowns"),
    }
    gates = brief.get("hard_gates")
    if not isinstance(gates, list) or not gates:
        raise ValidationError("hard_gates must be a non-empty array")
    normalized_gates: List[Dict[str, str]] = []
    seen_gate_ids: set[str] = set()
    for gate in gates:
        if not isinstance(gate, Mapping):
            raise ValidationError("each hard gate must be an object")
        gate_id = _require_string(gate, "id")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", gate_id):
            raise ValidationError(f"hard gate id is not canonical: {gate_id}")
        if gate_id in seen_gate_ids:
            raise ValidationError(f"duplicate hard gate id: {gate_id}")
        seen_gate_ids.add(gate_id)
        normalized_gates.append({"id": gate_id, "label": _require_string(gate, "label")})
    normalized["hard_gates"] = normalized_gates

    dimensions = brief.get("scoring_dimensions")
    if not isinstance(dimensions, list) or not dimensions:
        raise ValidationError("scoring_dimensions must be a non-empty array")
    normalized_dimensions: List[Dict[str, Any]] = []
    seen_dimension_ids: set[str] = set()
    total_weight = 0.0
    for dimension in dimensions:
        if not isinstance(dimension, Mapping):
            raise ValidationError("each scoring dimension must be an object")
        dimension_id = _require_string(dimension, "id")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", dimension_id):
            raise ValidationError(f"scoring dimension id is not canonical: {dimension_id}")
        if dimension_id in seen_dimension_ids:
            raise ValidationError(f"duplicate scoring dimension id: {dimension_id}")
        seen_dimension_ids.add(dimension_id)
        weight = dimension.get("weight")
        if not isinstance(weight, (int, float)) or float(weight) <= 0:
            raise ValidationError(f"scoring dimension {dimension_id} weight must be positive")
        direction = dimension.get("direction", "maximize")
        if direction not in {"maximize", "minimize"}:
            raise ValidationError(f"scoring dimension {dimension_id} direction must be maximize or minimize")
        total_weight += float(weight)
        normalized_dimensions.append(
            {
                "id": dimension_id,
                "label": _require_string(dimension, "label"),
                "weight": float(weight),
                "direction": direction,
            }
        )
    for dimension in normalized_dimensions:
        dimension["weight"] = dimension["weight"] / total_weight
    normalized["scoring_dimensions"] = normalized_dimensions
    return normalized


def create_run(
    project: Path | str,
    *,
    run_id: str,
    mode: str,
    brief: Mapping[str, Any],
    branch_budget: Optional[int] = None,
    minimum_branches: Optional[int] = None,
) -> Dict[str, Any]:
    run_id = safe_run_id(run_id)
    if mode not in MODE_DEFAULTS:
        raise ValidationError(f"mode must be one of {sorted(MODE_DEFAULTS)}")
    normalized_brief = _validate_brief(brief)
    defaults = MODE_DEFAULTS[mode]
    budget = int(branch_budget if branch_budget is not None else defaults["branch_budget"])
    minimum = int(minimum_branches if minimum_branches is not None else defaults["minimum_branches"])
    if minimum < 2:
        raise ValidationError("minimum_branches must be at least 2")
    if budget < minimum or budget > 100:
        raise ValidationError("branch_budget must be between minimum_branches and 100")
    directory = run_directory(project, run_id)
    if directory.exists() and (directory / DB_FILE).exists():
        raise ValidationError(f"deliberation run already exists: {run_id}")
    directory.mkdir(parents=True, exist_ok=True)
    _chmod_private(directory, directory=True)
    conn = _connect(directory)
    try:
        _ensure_schema(conn)
        now = utc_now()
        state: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "mode": mode,
            "phase": "framing",
            "revision": 0,
            "brief": normalized_brief,
            "limits": {
                "minimum_branches": minimum,
                "branch_budget": budget,
                "shortlist_size": min(defaults["shortlist_size"], budget),
            },
            "shortlist": [],
            "cursor": {
                "last_completed": "Created the deliberation run",
                "in_progress": "Framing the problem",
                "next_action": "Review the brief and transition to branching",
                "blockers": [],
            },
            "synthesis": None,
            "created_at": now,
            "updated_at": now,
        }
        with _transaction(conn):
            conn.execute(
                "INSERT INTO run_state(singleton, revision, document) VALUES (1, 0, ?)",
                (canonical_json(state),),
            )
        sync_exports(project, run_id, conn=conn)
        return state
    finally:
        conn.close()


def open_run(project: Path | str, run_id: str) -> Tuple[Path, sqlite3.Connection]:
    directory = run_directory(project, run_id)
    if not (directory / DB_FILE).is_file():
        raise ValidationError(f"deliberation run does not exist: {run_id}")
    conn = _connect(directory)
    _ensure_schema(conn)
    return directory, conn


def get_run_state(project: Path | str, run_id: str) -> Dict[str, Any]:
    _directory, conn = open_run(project, run_id)
    try:
        return _load_run_state(conn)
    finally:
        conn.close()


def _load_branch(conn: sqlite3.Connection, branch_id: str) -> Dict[str, Any]:
    if not BRANCH_ID_RE.fullmatch(branch_id):
        raise ValidationError("branch_id must match branch-NNN")
    row = conn.execute("SELECT document FROM branches WHERE branch_id = ?", (branch_id,)).fetchone()
    if row is None:
        raise ValidationError(f"branch not found: {branch_id}")
    return _load_json_document(row)


def list_branches(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute("SELECT document FROM branches ORDER BY branch_id").fetchall()
    return [_load_json_document(row) for row in rows]


def list_nodes(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute("SELECT document FROM nodes ORDER BY node_id").fetchall()
    return [_load_json_document(row) for row in rows]


def list_edges(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute("SELECT document FROM edges ORDER BY edge_id").fetchall()
    return [_load_json_document(row) for row in rows]


def list_critiques(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute("SELECT document FROM critiques ORDER BY critique_id").fetchall()
    return [_load_json_document(row) for row in rows]


def _put_branch(conn: sqlite3.Connection, branch: Mapping[str, Any]) -> None:
    conn.execute(
        "UPDATE branches SET status = ?, revision = ?, document = ?, updated_at = ? WHERE branch_id = ?",
        (
            branch["status"],
            int(branch["revision"]),
            canonical_json(branch),
            branch["updated_at"],
            branch["branch_id"],
        ),
    )


def _insert_node_locked(
    conn: sqlite3.Connection,
    *,
    node_type: str,
    title: str,
    summary: str,
    branch_id: Optional[str],
    status: str,
    source_refs: Sequence[str],
    tags: Sequence[str],
    data: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    if node_type not in NODE_TYPES:
        raise ValidationError(f"node_type must be one of {sorted(NODE_TYPES)}")
    if branch_id is not None:
        _load_branch(conn, branch_id)
    if node_type == "evidence" and status == "observed" and not source_refs:
        raise ValidationError("observed evidence requires at least one source reference")
    if node_type == "evidence" and status not in EVIDENCE_STATUSES:
        raise ValidationError(f"evidence status must be one of {sorted(EVIDENCE_STATUSES)}")
    _reject_forbidden_keys(data or {}, "node.data")
    node_id = _next_id(conn, "node_counter", "node")
    now = utc_now()
    node = {
        "schema_version": SCHEMA_VERSION,
        "node_id": node_id,
        "node_type": node_type,
        "branch_id": branch_id,
        "title": title.strip(),
        "summary": summary.strip(),
        "status": status,
        "source_refs": list(source_refs),
        "tags": list(tags),
        "data": copy.deepcopy(dict(data or {})),
        "revision": 1,
        "created_at": now,
        "updated_at": now,
    }
    conn.execute(
        "INSERT INTO nodes(node_id, node_type, branch_id, revision, document, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?, ?)",
        (node_id, node_type, branch_id, canonical_json(node), now, now),
    )
    return node


def add_branch(
    project: Path | str,
    *,
    run_id: str,
    strategy: str,
    distinguishing_assumption: str,
    optimization_target: str,
    known_tradeoff: str,
    introduced_by: str = "agent",
) -> Dict[str, Any]:
    payload = {
        "strategy": strategy,
        "distinguishing_assumption": distinguishing_assumption,
        "optimization_target": optimization_target,
        "known_tradeoff": known_tradeoff,
        "introduced_by": introduced_by,
    }
    _reject_forbidden_keys(payload)
    directory, conn = open_run(project, run_id)
    try:
        with _transaction(conn):
            state = _load_run_state(conn)
            if state["phase"] not in {"branching", "independent-exploration"}:
                raise ValidationError("branches can only be added during branching or independent-exploration")
            existing_count = conn.execute("SELECT COUNT(*) AS n FROM branches").fetchone()["n"]
            if int(existing_count) >= int(state["limits"]["branch_budget"]):
                raise ValidationError("branch budget is exhausted")
            normalized = {
                "strategy": _require_string(payload, "strategy"),
                "distinguishing_assumption": _require_string(payload, "distinguishing_assumption"),
                "optimization_target": _require_string(payload, "optimization_target"),
                "known_tradeoff": _require_string(payload, "known_tradeoff"),
                "introduced_by": _require_string(payload, "introduced_by"),
            }
            signature = _branch_signature(
                normalized["strategy"],
                normalized["distinguishing_assumption"],
                normalized["optimization_target"],
            )
            duplicate = conn.execute("SELECT branch_id FROM branches WHERE signature = ?", (signature,)).fetchone()
            if duplicate is not None:
                raise ValidationError(f"branch duplicates the distinguishing axes of {duplicate['branch_id']}")
            branch_id = _next_id(conn, "branch_counter", "branch", width=3)
            now = utc_now()
            branch: Dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "branch_id": branch_id,
                "strategy": normalized["strategy"],
                "distinguishing_assumption": normalized["distinguishing_assumption"],
                "optimization_target": normalized["optimization_target"],
                "known_tradeoff": normalized["known_tradeoff"],
                "introduced_by": normalized["introduced_by"],
                "status": "exploring" if state["phase"] == "independent-exploration" else "proposed",
                "summary": "",
                "assumptions": [],
                "risks": [],
                "next_tests": [],
                "changes": [],
                "remaining_weakness": "",
                "parent_branch_ids": [],
                "hard_gates": {},
                "scores": {},
                "weighted_score": None,
                "approach_node_id": None,
                "revision": 1,
                "created_at": now,
                "updated_at": now,
            }
            conn.execute(
                "INSERT INTO branches(branch_id, status, revision, signature, document, created_at, updated_at) VALUES (?, ?, 1, ?, ?, ?, ?)",
                (branch_id, branch["status"], signature, canonical_json(branch), now, now),
            )
            approach = _insert_node_locked(
                conn,
                node_type="approach",
                title=f"Approach {branch_id}",
                summary=normalized["strategy"],
                branch_id=branch_id,
                status="proposed",
                source_refs=[],
                tags=["branch-root"],
                data={
                    "distinguishing_assumption": normalized["distinguishing_assumption"],
                    "optimization_target": normalized["optimization_target"],
                    "known_tradeoff": normalized["known_tradeoff"],
                },
            )
            branch["approach_node_id"] = approach["node_id"]
            _put_branch(conn, branch)
            _bump_run(state)
            state["cursor"] = {
                "last_completed": f"Added {branch_id}",
                "in_progress": "Generating distinct solution branches",
                "next_action": "Add another distinct branch or start independent exploration",
                "blockers": [],
            }
            _put_run_state(conn, state)
        sync_exports(project, run_id, conn=conn)
        return branch
    except sqlite3.IntegrityError as exc:
        raise ValidationError(f"branch could not be inserted: {exc}") from exc
    finally:
        conn.close()


def add_node(
    project: Path | str,
    *,
    run_id: str,
    node_type: str,
    title: str,
    summary: str,
    branch_id: Optional[str] = None,
    status: str = "active",
    source_refs: Optional[Sequence[str]] = None,
    tags: Optional[Sequence[str]] = None,
    data: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    if not isinstance(title, str) or not title.strip():
        raise ValidationError("node title must be a non-empty string")
    if not isinstance(summary, str) or not summary.strip():
        raise ValidationError("node summary must be a non-empty string")
    refs = _string_list(list(source_refs or []), "source_refs")
    normalized_tags = _string_list(list(tags or []), "tags")
    _directory, conn = open_run(project, run_id)
    try:
        with _transaction(conn):
            state = _load_run_state(conn)
            if state["phase"] == "complete":
                raise ValidationError("completed deliberation runs are immutable")
            node = _insert_node_locked(
                conn,
                node_type=node_type,
                title=title,
                summary=summary,
                branch_id=branch_id,
                status=status,
                source_refs=refs,
                tags=normalized_tags,
                data=data,
            )
            _bump_run(state)
            _put_run_state(conn, state)
        sync_exports(project, run_id, conn=conn)
        return node
    finally:
        conn.close()


def add_edge(
    project: Path | str,
    *,
    run_id: str,
    source_id: str,
    target_id: str,
    relation: str,
    rationale: str = "",
) -> Dict[str, Any]:
    if relation not in EDGE_TYPES:
        raise ValidationError(f"relation must be one of {sorted(EDGE_TYPES)}")
    if source_id == target_id:
        raise ValidationError("self-edges are not permitted")
    _reject_forbidden_keys({"rationale": rationale})
    _directory, conn = open_run(project, run_id)
    try:
        with _transaction(conn):
            state = _load_run_state(conn)
            for node_id in (source_id, target_id):
                if conn.execute("SELECT 1 FROM nodes WHERE node_id = ?", (node_id,)).fetchone() is None:
                    raise ValidationError(f"edge endpoint does not exist: {node_id}")
            edge_id = _next_id(conn, "edge_counter", "edge")
            now = utc_now()
            edge = {
                "schema_version": SCHEMA_VERSION,
                "edge_id": edge_id,
                "source_id": source_id,
                "target_id": target_id,
                "relation": relation,
                "rationale": str(rationale),
                "created_at": now,
            }
            conn.execute(
                "INSERT INTO edges(edge_id, source_id, target_id, relation, document, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (edge_id, source_id, target_id, relation, canonical_json(edge), now),
            )
            _bump_run(state)
            _put_run_state(conn, state)
            if relation in DEPENDENCY_RELATIONS:
                _assert_no_dependency_cycle(conn)
        sync_exports(project, run_id, conn=conn)
        return edge
    except sqlite3.IntegrityError as exc:
        raise ValidationError(f"duplicate or invalid edge: {exc}") from exc
    finally:
        conn.close()


def complete_first_pass(
    project: Path | str,
    *,
    run_id: str,
    branch_id: str,
    expected_revision: int,
    summary: str,
    assumptions: Sequence[str],
    risks: Sequence[str],
    next_tests: Sequence[str],
) -> Dict[str, Any]:
    payload = {
        "summary": summary,
        "assumptions": list(assumptions),
        "risks": list(risks),
        "next_tests": list(next_tests),
    }
    _reject_forbidden_keys(payload)
    _directory, conn = open_run(project, run_id)
    try:
        with _transaction(conn):
            state = _load_run_state(conn)
            if state["phase"] != "independent-exploration":
                raise ValidationError("first-pass completion is only allowed during independent-exploration")
            branch = _load_branch(conn, branch_id)
            if int(branch["revision"]) != int(expected_revision):
                raise RevisionConflict(
                    f"stale branch revision {expected_revision}; current revision is {branch['revision']}"
                )
            if branch["status"] not in {"proposed", "exploring"}:
                raise ValidationError(f"branch {branch_id} is not awaiting a first pass")
            branch.update(
                {
                    "summary": _require_string(payload, "summary"),
                    "assumptions": _string_list(payload["assumptions"], "assumptions", allow_empty=False),
                    "risks": _string_list(payload["risks"], "risks", allow_empty=False),
                    "next_tests": _string_list(payload["next_tests"], "next_tests"),
                    "status": "first-pass-complete",
                    "revision": int(branch["revision"]) + 1,
                    "updated_at": utc_now(),
                }
            )
            _put_branch(conn, branch)
            approach = conn.execute(
                "SELECT document FROM nodes WHERE node_id = ?", (branch["approach_node_id"],)
            ).fetchone()
            if approach is not None:
                node = _load_json_document(approach)
                node.update(
                    {
                        "summary": branch["summary"],
                        "status": "first-pass-complete",
                        "revision": int(node["revision"]) + 1,
                        "updated_at": utc_now(),
                    }
                )
                conn.execute(
                    "UPDATE nodes SET revision = ?, document = ?, updated_at = ? WHERE node_id = ?",
                    (node["revision"], canonical_json(node), node["updated_at"], node["node_id"]),
                )
            _bump_run(state)
            state["cursor"] = {
                "last_completed": f"Completed independent first pass for {branch_id}",
                "in_progress": "Completing remaining independent branches",
                "next_action": "Complete another branch first pass or transition to evidence",
                "blockers": [],
            }
            _put_run_state(conn, state)
        sync_exports(project, run_id, conn=conn)
        return branch
    finally:
        conn.close()


def set_hard_gates(
    project: Path | str,
    *,
    run_id: str,
    branch_id: str,
    expected_revision: int,
    gates: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    _reject_forbidden_keys(gates, "gates")
    _directory, conn = open_run(project, run_id)
    try:
        with _transaction(conn):
            state = _load_run_state(conn)
            if state["phase"] not in {"evidence", "cross-critique", "revision"}:
                raise ValidationError("hard gates may only be evaluated from the evidence phase onward")
            branch = _load_branch(conn, branch_id)
            if int(branch["revision"]) != int(expected_revision):
                raise RevisionConflict(
                    f"stale branch revision {expected_revision}; current revision is {branch['revision']}"
                )
            required_ids = [item["id"] for item in state["brief"]["hard_gates"]]
            if set(gates) != set(required_ids):
                raise ValidationError(
                    f"hard gate keys must exactly match {sorted(required_ids)}; got {sorted(gates)}"
                )
            normalized: Dict[str, Any] = {}
            for gate_id in required_ids:
                item = gates[gate_id]
                if not isinstance(item, Mapping) or not isinstance(item.get("passed"), bool):
                    raise ValidationError(f"hard gate {gate_id} must contain boolean passed")
                evidence_ids = _string_list(list(item.get("evidence_ids", [])), f"hard gate {gate_id} evidence_ids")
                for node_id in evidence_ids:
                    row = conn.execute("SELECT node_type FROM nodes WHERE node_id = ?", (node_id,)).fetchone()
                    if row is None or row["node_type"] not in {"evidence", "experiment"}:
                        raise ValidationError(f"hard gate {gate_id} cites non-evidence node {node_id}")
                normalized[gate_id] = {
                    "passed": bool(item["passed"]),
                    "evidence_ids": evidence_ids,
                    "reason": _require_string(item, "reason"),
                }
            branch.update(
                {
                    "hard_gates": normalized,
                    "revision": int(branch["revision"]) + 1,
                    "updated_at": utc_now(),
                }
            )
            _put_branch(conn, branch)
            _bump_run(state)
            _put_run_state(conn, state)
        sync_exports(project, run_id, conn=conn)
        return branch
    finally:
        conn.close()


def score_branch(
    project: Path | str,
    *,
    run_id: str,
    branch_id: str,
    expected_revision: int,
    scores: Mapping[str, float],
    rationale: str,
) -> Dict[str, Any]:
    _reject_forbidden_keys({"scores": scores, "rationale": rationale})
    _directory, conn = open_run(project, run_id)
    try:
        with _transaction(conn):
            state = _load_run_state(conn)
            if state["phase"] not in {"evidence", "cross-critique", "revision"}:
                raise ValidationError("branches may only be scored from the evidence phase onward")
            branch = _load_branch(conn, branch_id)
            if int(branch["revision"]) != int(expected_revision):
                raise RevisionConflict(
                    f"stale branch revision {expected_revision}; current revision is {branch['revision']}"
                )
            required_gates = {item["id"] for item in state["brief"]["hard_gates"]}
            if set(branch.get("hard_gates", {})) != required_gates:
                raise ValidationError("all hard gates must be evaluated before scoring")
            failed = [gate_id for gate_id, item in branch["hard_gates"].items() if not item["passed"]]
            if failed:
                raise ValidationError(f"branch failed hard gates and cannot be scored: {', '.join(sorted(failed))}")
            dimensions = state["brief"]["scoring_dimensions"]
            required = {item["id"] for item in dimensions}
            if set(scores) != required:
                raise ValidationError(f"score keys must exactly match {sorted(required)}")
            normalized: Dict[str, float] = {}
            weighted = 0.0
            for dimension in dimensions:
                raw = scores[dimension["id"]]
                if not isinstance(raw, (int, float)) or not 0 <= float(raw) <= 1:
                    raise ValidationError(f"score {dimension['id']} must be between 0 and 1")
                value = float(raw)
                normalized[dimension["id"]] = value
                utility = value if dimension["direction"] == "maximize" else 1.0 - value
                weighted += utility * float(dimension["weight"])
            branch.update(
                {
                    "scores": normalized,
                    "score_rationale": str(rationale),
                    "weighted_score": round(weighted, 8),
                    "revision": int(branch["revision"]) + 1,
                    "updated_at": utc_now(),
                }
            )
            _put_branch(conn, branch)
            _bump_run(state)
            _put_run_state(conn, state)
        sync_exports(project, run_id, conn=conn)
        return branch
    finally:
        conn.close()


def set_shortlist(
    project: Path | str,
    *,
    run_id: str,
    branch_ids: Sequence[str],
    expected_run_revision: int,
) -> Dict[str, Any]:
    normalized_ids = _string_list(list(branch_ids), "branch_ids", allow_empty=False)
    if len(normalized_ids) < 2:
        raise ValidationError("shortlist must contain at least two branches")
    _directory, conn = open_run(project, run_id)
    try:
        with _transaction(conn):
            state = _load_run_state(conn)
            if int(state["revision"]) != int(expected_run_revision):
                raise RevisionConflict(
                    f"stale run revision {expected_run_revision}; current revision is {state['revision']}"
                )
            if state["phase"] != "evidence":
                raise ValidationError("shortlist is selected during the evidence phase")
            if len(normalized_ids) > int(state["limits"]["shortlist_size"]):
                raise ValidationError("shortlist exceeds the configured shortlist size")
            branches = {item["branch_id"]: item for item in list_branches(conn)}
            for branch_id in normalized_ids:
                branch = branches.get(branch_id)
                if branch is None:
                    raise ValidationError(f"shortlist branch does not exist: {branch_id}")
                if branch.get("weighted_score") is None:
                    raise ValidationError(f"shortlist branch has not been scored: {branch_id}")
                if not branch.get("hard_gates") or not all(
                    item.get("passed") is True for item in branch["hard_gates"].values()
                ):
                    raise ValidationError(f"shortlist branch has not passed all hard gates: {branch_id}")
            for branch_id in normalized_ids:
                branch = branches[branch_id]
                branch.update(
                    {
                        "status": "shortlisted",
                        "revision": int(branch["revision"]) + 1,
                        "updated_at": utc_now(),
                    }
                )
                _put_branch(conn, branch)
            state["shortlist"] = normalized_ids
            _bump_run(state)
            state["cursor"] = {
                "last_completed": "Selected the strongest evidence-qualified branches",
                "in_progress": "Preparing cross-critique",
                "next_action": "Transition to cross-critique and attack each shortlisted branch",
                "blockers": [],
            }
            _put_run_state(conn, state)
        sync_exports(project, run_id, conn=conn)
        return state
    finally:
        conn.close()


def add_critique(
    project: Path | str,
    *,
    run_id: str,
    branch_id: str,
    critic_branch_id: str,
    attack_type: str,
    summary: str,
    severity: str,
    source_refs: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    if attack_type not in ATTACK_TYPES:
        raise ValidationError(f"attack_type must be one of {sorted(ATTACK_TYPES)}")
    if severity not in {"low", "medium", "high", "blocking"}:
        raise ValidationError("severity must be low, medium, high, or blocking")
    if critic_branch_id != "independent" and critic_branch_id == branch_id:
        raise ValidationError("a branch cannot act as its own critic")
    refs = _string_list(list(source_refs or []), "source_refs")
    _reject_forbidden_keys({"summary": summary})
    _directory, conn = open_run(project, run_id)
    try:
        with _transaction(conn):
            state = _load_run_state(conn)
            if state["phase"] != "cross-critique":
                raise ValidationError("critiques can only be added during cross-critique")
            if branch_id not in state.get("shortlist", []):
                raise ValidationError("critiques must target a shortlisted branch")
            _load_branch(conn, branch_id)
            if critic_branch_id != "independent":
                _load_branch(conn, critic_branch_id)
            critique_id = _next_id(conn, "critique_counter", "critique")
            now = utc_now()
            critique = {
                "schema_version": SCHEMA_VERSION,
                "critique_id": critique_id,
                "branch_id": branch_id,
                "critic_branch_id": critic_branch_id,
                "attack_type": attack_type,
                "summary": summary.strip(),
                "severity": severity,
                "status": "open",
                "source_refs": refs,
                "response": "",
                "remaining_weakness": "",
                "revision": 1,
                "created_at": now,
                "updated_at": now,
            }
            if not critique["summary"]:
                raise ValidationError("critique summary must be non-empty")
            conn.execute(
                "INSERT INTO critiques(critique_id, branch_id, critic_branch_id, status, revision, document, created_at, updated_at) VALUES (?, ?, ?, 'open', 1, ?, ?, ?)",
                (critique_id, branch_id, critic_branch_id, canonical_json(critique), now, now),
            )
            _bump_run(state)
            _put_run_state(conn, state)
        sync_exports(project, run_id, conn=conn)
        return critique
    finally:
        conn.close()


def respond_to_critique(
    project: Path | str,
    *,
    run_id: str,
    critique_id: str,
    expected_revision: int,
    response: str,
    remaining_weakness: str,
    accept: bool = False,
) -> Dict[str, Any]:
    if not CRITIQUE_ID_RE.fullmatch(critique_id):
        raise ValidationError("critique_id must match critique-NNNNNN")
    _reject_forbidden_keys({"response": response, "remaining_weakness": remaining_weakness})
    _directory, conn = open_run(project, run_id)
    try:
        with _transaction(conn):
            state = _load_run_state(conn)
            if state["phase"] not in {"revision", "cross-critique"}:
                raise ValidationError("critique responses are allowed during cross-critique or revision")
            row = conn.execute("SELECT document FROM critiques WHERE critique_id = ?", (critique_id,)).fetchone()
            if row is None:
                raise ValidationError(f"critique not found: {critique_id}")
            critique = _load_json_document(row)
            if int(critique["revision"]) != int(expected_revision):
                raise RevisionConflict(
                    f"stale critique revision {expected_revision}; current revision is {critique['revision']}"
                )
            critique.update(
                {
                    "response": response.strip(),
                    "remaining_weakness": remaining_weakness.strip(),
                    "status": "accepted" if accept else "answered",
                    "revision": int(critique["revision"]) + 1,
                    "updated_at": utc_now(),
                }
            )
            if not critique["response"]:
                raise ValidationError("critique response must be non-empty")
            conn.execute(
                "UPDATE critiques SET status = ?, revision = ?, document = ?, updated_at = ? WHERE critique_id = ?",
                (
                    critique["status"],
                    critique["revision"],
                    canonical_json(critique),
                    critique["updated_at"],
                    critique_id,
                ),
            )
            _bump_run(state)
            _put_run_state(conn, state)
        sync_exports(project, run_id, conn=conn)
        return critique
    finally:
        conn.close()


def revise_branch(
    project: Path | str,
    *,
    run_id: str,
    branch_id: str,
    expected_revision: int,
    summary: str,
    changes: Sequence[str],
    remaining_weakness: str,
    assumptions: Optional[Sequence[str]] = None,
    risks: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    payload = {
        "summary": summary,
        "changes": list(changes),
        "remaining_weakness": remaining_weakness,
        "assumptions": list(assumptions) if assumptions is not None else None,
        "risks": list(risks) if risks is not None else None,
    }
    _reject_forbidden_keys(payload)
    _directory, conn = open_run(project, run_id)
    try:
        with _transaction(conn):
            state = _load_run_state(conn)
            if state["phase"] != "revision":
                raise ValidationError("branches can only be revised during the revision phase")
            if branch_id not in state.get("shortlist", []):
                raise ValidationError("only shortlisted branches may be revised")
            branch = _load_branch(conn, branch_id)
            if int(branch["revision"]) != int(expected_revision):
                raise RevisionConflict(
                    f"stale branch revision {expected_revision}; current revision is {branch['revision']}"
                )
            critiques = [item for item in list_critiques(conn) if item["branch_id"] == branch_id]
            if not critiques:
                raise ValidationError("a shortlisted branch must receive a critique before revision")
            open_ids = [item["critique_id"] for item in critiques if item["status"] == "open"]
            if open_ids:
                raise ValidationError(f"branch still has open critiques: {', '.join(open_ids)}")
            branch.update(
                {
                    "summary": _require_string(payload, "summary"),
                    "changes": _string_list(payload["changes"], "changes", allow_empty=False),
                    "remaining_weakness": _require_string(payload, "remaining_weakness"),
                    "status": "revised",
                    "revision": int(branch["revision"]) + 1,
                    "updated_at": utc_now(),
                }
            )
            if assumptions is not None:
                branch["assumptions"] = _string_list(list(assumptions), "assumptions", allow_empty=False)
            if risks is not None:
                branch["risks"] = _string_list(list(risks), "risks", allow_empty=False)
            _put_branch(conn, branch)
            _bump_run(state)
            state["cursor"] = {
                "last_completed": f"Revised {branch_id} after critique",
                "in_progress": "Revising shortlisted branches",
                "next_action": "Resolve and revise the remaining shortlisted branches",
                "blockers": [],
            }
            _put_run_state(conn, state)
        sync_exports(project, run_id, conn=conn)
        return branch
    finally:
        conn.close()


def merge_branches(
    project: Path | str,
    *,
    run_id: str,
    parent_branch_ids: Sequence[str],
    strategy: str,
    distinguishing_assumption: str,
    optimization_target: str,
    known_tradeoff: str,
) -> Dict[str, Any]:
    parents = _string_list(list(parent_branch_ids), "parent_branch_ids", allow_empty=False)
    if len(parents) < 2:
        raise ValidationError("a merge requires at least two parent branches")
    _directory, conn = open_run(project, run_id)
    try:
        with _transaction(conn):
            state = _load_run_state(conn)
            if state["phase"] not in {"evidence", "cross-critique", "revision"}:
                raise ValidationError("branches may only be merged after independent exploration")
            parent_docs = [_load_branch(conn, branch_id) for branch_id in parents]
            if any(item["status"] in {"proposed", "exploring"} for item in parent_docs):
                raise ValidationError("all merge parents must have completed a first pass")
            existing_count = conn.execute("SELECT COUNT(*) AS n FROM branches").fetchone()["n"]
            if int(existing_count) >= int(state["limits"]["branch_budget"]):
                raise ValidationError("branch budget is exhausted")
            normalized = {
                "strategy": strategy,
                "distinguishing_assumption": distinguishing_assumption,
                "optimization_target": optimization_target,
                "known_tradeoff": known_tradeoff,
            }
            for key in normalized:
                normalized[key] = _require_string(normalized, key)
            signature = _branch_signature(
                normalized["strategy"], normalized["distinguishing_assumption"], normalized["optimization_target"]
            )
            duplicate = conn.execute("SELECT branch_id FROM branches WHERE signature = ?", (signature,)).fetchone()
            if duplicate is not None:
                raise ValidationError(f"merged branch duplicates {duplicate['branch_id']}")
            branch_id = _next_id(conn, "branch_counter", "branch", width=3)
            now = utc_now()
            branch = {
                "schema_version": SCHEMA_VERSION,
                "branch_id": branch_id,
                "strategy": normalized["strategy"],
                "distinguishing_assumption": normalized["distinguishing_assumption"],
                "optimization_target": normalized["optimization_target"],
                "known_tradeoff": normalized["known_tradeoff"],
                "introduced_by": "merge",
                "status": "merged",
                "summary": normalized["strategy"],
                "assumptions": [],
                "risks": [],
                "next_tests": [],
                "changes": [f"Merged {', '.join(parents)}"],
                "remaining_weakness": "Requires independent evaluation after merge",
                "parent_branch_ids": parents,
                "hard_gates": {},
                "scores": {},
                "weighted_score": None,
                "approach_node_id": None,
                "revision": 1,
                "created_at": now,
                "updated_at": now,
            }
            conn.execute(
                "INSERT INTO branches(branch_id, status, revision, signature, document, created_at, updated_at) VALUES (?, ?, 1, ?, ?, ?, ?)",
                (branch_id, branch["status"], signature, canonical_json(branch), now, now),
            )
            approach = _insert_node_locked(
                conn,
                node_type="approach",
                title=f"Merged approach {branch_id}",
                summary=normalized["strategy"],
                branch_id=branch_id,
                status="merged",
                source_refs=[],
                tags=["branch-root", "merge"],
                data={"parent_branch_ids": parents},
            )
            branch["approach_node_id"] = approach["node_id"]
            _put_branch(conn, branch)
            for parent in parent_docs:
                edge_id = _next_id(conn, "edge_counter", "edge")
                edge = {
                    "schema_version": SCHEMA_VERSION,
                    "edge_id": edge_id,
                    "source_id": parent["approach_node_id"],
                    "target_id": approach["node_id"],
                    "relation": "merges_with",
                    "rationale": f"{parent['branch_id']} contributes to {branch_id}",
                    "created_at": now,
                }
                conn.execute(
                    "INSERT INTO edges(edge_id, source_id, target_id, relation, document, created_at) VALUES (?, ?, ?, 'merges_with', ?, ?)",
                    (edge_id, edge["source_id"], edge["target_id"], canonical_json(edge), now),
                )
            _bump_run(state)
            _put_run_state(conn, state)
        sync_exports(project, run_id, conn=conn)
        return branch
    finally:
        conn.close()


def transition_phase(
    project: Path | str,
    *,
    run_id: str,
    target_phase: str,
    expected_run_revision: int,
) -> Dict[str, Any]:
    if target_phase not in PHASES:
        raise ValidationError(f"target_phase must be one of {PHASES}")
    _directory, conn = open_run(project, run_id)
    try:
        with _transaction(conn):
            state = _load_run_state(conn)
            if int(state["revision"]) != int(expected_run_revision):
                raise RevisionConflict(
                    f"stale run revision {expected_run_revision}; current revision is {state['revision']}"
                )
            current_index = PHASES.index(state["phase"])
            target_index = PHASES.index(target_phase)
            if target_index != current_index + 1:
                raise ValidationError("phase transitions must move exactly one step forward")
            branches = list_branches(conn)
            if target_phase == "independent-exploration":
                if len(branches) < int(state["limits"]["minimum_branches"]):
                    raise ValidationError("minimum branch count has not been reached")
                for branch in branches:
                    if branch["status"] == "proposed":
                        branch.update(
                            {
                                "status": "exploring",
                                "revision": int(branch["revision"]) + 1,
                                "updated_at": utc_now(),
                            }
                        )
                        _put_branch(conn, branch)
            elif target_phase == "evidence":
                unfinished = [
                    branch["branch_id"]
                    for branch in branches
                    if branch["status"] in {"proposed", "exploring"}
                ]
                if unfinished:
                    raise ValidationError(
                        "all active branches must finish an independent first pass: " + ", ".join(unfinished)
                    )
            elif target_phase == "cross-critique":
                if len(state.get("shortlist", [])) < 2:
                    raise ValidationError("select at least two evidence-qualified branches before cross-critique")
            elif target_phase == "revision":
                critiques = list_critiques(conn)
                missing = [
                    branch_id
                    for branch_id in state.get("shortlist", [])
                    if not any(item["branch_id"] == branch_id for item in critiques)
                ]
                if missing:
                    raise ValidationError("each shortlisted branch needs a critique: " + ", ".join(missing))
            elif target_phase == "synthesis":
                critiques = list_critiques(conn)
                open_items = [item["critique_id"] for item in critiques if item["status"] == "open"]
                if open_items:
                    raise ValidationError("all critiques must be answered or accepted before synthesis")
                branches_by_id = {item["branch_id"]: item for item in branches}
                not_revised = [
                    branch_id
                    for branch_id in state.get("shortlist", [])
                    if branches_by_id.get(branch_id, {}).get("status") != "revised"
                ]
                if not_revised:
                    raise ValidationError("all shortlisted branches must be revised: " + ", ".join(not_revised))
            elif target_phase == "complete":
                if not isinstance(state.get("synthesis"), Mapping):
                    raise ValidationError("a complete synthesis is required before marking the run complete")
            state["phase"] = target_phase
            _bump_run(state)
            state["cursor"] = _phase_cursor(target_phase)
            _put_run_state(conn, state)
        sync_exports(project, run_id, conn=conn)
        return state
    finally:
        conn.close()


def _phase_cursor(phase: str) -> Dict[str, Any]:
    mapping = {
        "branching": ("Framed the problem", "Generating distinct branches", "Add branches with distinct assumptions and optimization targets"),
        "independent-exploration": ("Created the branch set", "Exploring branches independently", "Complete each branch's first pass without cross-branch conclusions"),
        "evidence": ("Completed independent first passes", "Testing assumptions and hard gates", "Attach evidence, evaluate gates, and score viable branches"),
        "cross-critique": ("Selected the evidence-qualified shortlist", "Attacking shortlisted branches", "Add at least one concrete critique for each shortlisted branch"),
        "revision": ("Completed cross-critique", "Answering critiques and revising branches", "Resolve critiques and revise every shortlisted branch"),
        "synthesis": ("Revised the shortlisted branches", "Synthesizing the decision", "Choose a primary and fallback branch with reversal evidence and a surviving caveat"),
        "complete": ("Recorded the final synthesis", "Run complete", "Execute or communicate the selected decision"),
    }
    last, progress, next_action = mapping.get(phase, ("Updated phase", phase, "Continue the deliberation"))
    return {"last_completed": last, "in_progress": progress, "next_action": next_action, "blockers": []}


def checkpoint(
    project: Path | str,
    *,
    run_id: str,
    expected_run_revision: int,
    last_completed: str,
    in_progress: str,
    next_action: str,
    blockers: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    payload = {
        "last_completed": last_completed,
        "in_progress": in_progress,
        "next_action": next_action,
        "blockers": list(blockers or []),
    }
    _reject_forbidden_keys(payload)
    _directory, conn = open_run(project, run_id)
    try:
        with _transaction(conn):
            state = _load_run_state(conn)
            if int(state["revision"]) != int(expected_run_revision):
                raise RevisionConflict(
                    f"stale run revision {expected_run_revision}; current revision is {state['revision']}"
                )
            state["cursor"] = {
                "last_completed": _require_string(payload, "last_completed"),
                "in_progress": _require_string(payload, "in_progress"),
                "next_action": _require_string(payload, "next_action"),
                "blockers": _string_list(payload["blockers"], "blockers"),
            }
            _bump_run(state)
            _put_run_state(conn, state)
        sync_exports(project, run_id, conn=conn)
        return state
    finally:
        conn.close()


def synthesize(
    project: Path | str,
    *,
    run_id: str,
    expected_run_revision: int,
    selected_branch_id: str,
    fallback_branch_id: str,
    rationale: str,
    residual_assumptions: Sequence[str],
    reversal_evidence: Sequence[str],
    rejected_reasons: Mapping[str, str],
    surviving_caveat: str,
) -> Dict[str, Any]:
    payload = {
        "rationale": rationale,
        "residual_assumptions": list(residual_assumptions),
        "reversal_evidence": list(reversal_evidence),
        "rejected_reasons": dict(rejected_reasons),
        "surviving_caveat": surviving_caveat,
    }
    _reject_forbidden_keys(payload)
    if selected_branch_id == fallback_branch_id:
        raise ValidationError("selected and fallback branches must differ")
    _directory, conn = open_run(project, run_id)
    try:
        with _transaction(conn):
            state = _load_run_state(conn)
            if int(state["revision"]) != int(expected_run_revision):
                raise RevisionConflict(
                    f"stale run revision {expected_run_revision}; current revision is {state['revision']}"
                )
            if state["phase"] != "synthesis":
                raise ValidationError("synthesis can only be recorded during the synthesis phase")
            branches = {item["branch_id"]: item for item in list_branches(conn)}
            for branch_id, label in ((selected_branch_id, "selected"), (fallback_branch_id, "fallback")):
                branch = branches.get(branch_id)
                if branch is None:
                    raise ValidationError(f"{label} branch does not exist: {branch_id}")
                if branch.get("weighted_score") is None or not branch.get("hard_gates") or not all(
                    item.get("passed") is True for item in branch["hard_gates"].values()
                ):
                    raise ValidationError(f"{label} branch has not passed gates and scoring")
                if branch["status"] != "revised":
                    raise ValidationError(f"{label} branch must be revised before synthesis")
            other_ids = sorted(set(branches) - {selected_branch_id, fallback_branch_id})
            if set(rejected_reasons) != set(other_ids):
                raise ValidationError(
                    f"rejected_reasons must exactly cover non-selected branches: {other_ids}"
                )
            for branch_id, reason in rejected_reasons.items():
                if not isinstance(reason, str) or not reason.strip():
                    raise ValidationError(f"rejected reason for {branch_id} must be non-empty")
            synthesis_doc = {
                "selected_branch_id": selected_branch_id,
                "fallback_branch_id": fallback_branch_id,
                "rationale": _require_string(payload, "rationale"),
                "residual_assumptions": _string_list(payload["residual_assumptions"], "residual_assumptions", allow_empty=False),
                "reversal_evidence": _string_list(payload["reversal_evidence"], "reversal_evidence", allow_empty=False),
                "rejected_reasons": {key: value.strip() for key, value in sorted(rejected_reasons.items())},
                "surviving_caveat": _require_string(payload, "surviving_caveat"),
                "created_at": utc_now(),
            }
            for branch_id, branch in branches.items():
                if branch_id == selected_branch_id:
                    branch["status"] = "selected"
                elif branch_id == fallback_branch_id:
                    branch["status"] = "fallback"
                else:
                    branch["status"] = "rejected"
                branch["revision"] = int(branch["revision"]) + 1
                branch["updated_at"] = utc_now()
                _put_branch(conn, branch)
            selected_node = branches[selected_branch_id]["approach_node_id"]
            for branch_id, branch in branches.items():
                if branch_id == selected_branch_id:
                    continue
                edge_id = _next_id(conn, "edge_counter", "edge")
                edge = {
                    "schema_version": SCHEMA_VERSION,
                    "edge_id": edge_id,
                    "source_id": selected_node,
                    "target_id": branch["approach_node_id"],
                    "relation": "selected_over",
                    "rationale": (
                        "Fallback retained" if branch_id == fallback_branch_id else rejected_reasons[branch_id]
                    ),
                    "created_at": utc_now(),
                }
                conn.execute(
                    "INSERT INTO edges(edge_id, source_id, target_id, relation, document, created_at) VALUES (?, ?, ?, 'selected_over', ?, ?)",
                    (edge_id, edge["source_id"], edge["target_id"], canonical_json(edge), edge["created_at"]),
                )
            state["synthesis"] = synthesis_doc
            _bump_run(state)
            state["cursor"] = {
                "last_completed": "Recorded the selected and fallback branches",
                "in_progress": "Reviewing the final synthesis",
                "next_action": "Transition the run to complete",
                "blockers": [],
            }
            _put_run_state(conn, state)
        sync_exports(project, run_id, conn=conn)
        return state
    finally:
        conn.close()


def _assert_no_dependency_cycle(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT source_id, target_id, relation FROM edges WHERE relation IN ('depends_on', 'derived_from')"
    ).fetchall()
    graph: Dict[str, List[str]] = {}
    for row in rows:
        graph.setdefault(str(row["source_id"]), []).append(str(row["target_id"]))
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise ValidationError("dependency edges contain a cycle")
        if node in visited:
            return
        visiting.add(node)
        for neighbor in graph.get(node, []):
            visit(neighbor)
        visiting.remove(node)
        visited.add(node)

    for node in sorted(graph):
        visit(node)


def _validate_phase_invariants(
    state: Mapping[str, Any], branches: Sequence[Mapping[str, Any]], critiques: Sequence[Mapping[str, Any]]
) -> List[str]:
    errors: List[str] = []
    phase = state.get("phase")
    if phase not in PHASES:
        errors.append(f"unknown phase: {phase!r}")
        return errors
    if PHASES.index(str(phase)) >= PHASES.index("independent-exploration"):
        if len(branches) < int(state.get("limits", {}).get("minimum_branches", 2)):
            errors.append("phase requires the configured minimum number of branches")
    if PHASES.index(str(phase)) >= PHASES.index("evidence"):
        unfinished = [item["branch_id"] for item in branches if item.get("status") in {"proposed", "exploring"}]
        if unfinished:
            errors.append("branches remain without an independent first pass: " + ", ".join(unfinished))
    if PHASES.index(str(phase)) >= PHASES.index("cross-critique") and len(state.get("shortlist", [])) < 2:
        errors.append("cross-critique and later phases require a shortlist")
    if PHASES.index(str(phase)) >= PHASES.index("revision"):
        missing = [
            branch_id
            for branch_id in state.get("shortlist", [])
            if not any(item.get("branch_id") == branch_id for item in critiques)
        ]
        if missing:
            errors.append("shortlisted branches missing critiques: " + ", ".join(missing))
    if phase == "complete" and not isinstance(state.get("synthesis"), Mapping):
        errors.append("complete run has no synthesis")
    return errors


def validate_run(project: Path | str, run_id: str) -> Dict[str, Any]:
    directory, conn = open_run(project, run_id)
    errors: List[str] = []
    warnings: List[str] = []
    checks: Dict[str, Any] = {}
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        checks["sqlite_integrity"] = integrity
        if integrity != "ok":
            errors.append(f"SQLite integrity check failed: {integrity}")
        state = _load_run_state(conn)
        branches = list_branches(conn)
        nodes = list_nodes(conn)
        edges = list_edges(conn)
        critiques = list_critiques(conn)
        checks.update(
            {
                "phase": state.get("phase"),
                "run_revision": state.get("revision"),
                "branch_count": len(branches),
                "node_count": len(nodes),
                "edge_count": len(edges),
                "critique_count": len(critiques),
            }
        )
        _reject_forbidden_keys(state, "stored.run")
        for branch in branches:
            _reject_forbidden_keys(branch, f"stored.{branch.get('branch_id')}")
            if branch.get("status") not in BRANCH_STATUSES:
                errors.append(f"branch {branch.get('branch_id')} has invalid status")
            if not BRANCH_ID_RE.fullmatch(str(branch.get("branch_id", ""))):
                errors.append(f"invalid branch id: {branch.get('branch_id')}")
        branch_ids = {str(item["branch_id"]) for item in branches}
        node_ids = {str(item["node_id"]) for item in nodes}
        for node in nodes:
            _reject_forbidden_keys(node, f"stored.{node.get('node_id')}")
            if node.get("node_type") not in NODE_TYPES:
                errors.append(f"node {node.get('node_id')} has invalid type")
            if node.get("branch_id") is not None and node.get("branch_id") not in branch_ids:
                errors.append(f"node {node.get('node_id')} references missing branch")
        for edge in edges:
            if edge.get("source_id") not in node_ids or edge.get("target_id") not in node_ids:
                errors.append(f"edge {edge.get('edge_id')} references missing node")
            if edge.get("relation") not in EDGE_TYPES:
                errors.append(f"edge {edge.get('edge_id')} has invalid relation")
        try:
            _assert_no_dependency_cycle(conn)
        except ValidationError as exc:
            errors.append(str(exc))
        for critique in critiques:
            _reject_forbidden_keys(critique, f"stored.{critique.get('critique_id')}")
            if critique.get("branch_id") not in branch_ids:
                errors.append(f"critique {critique.get('critique_id')} references missing branch")
            if critique.get("status") not in CRITIQUE_STATUSES:
                errors.append(f"critique {critique.get('critique_id')} has invalid status")
        errors.extend(_validate_phase_invariants(state, branches, critiques))
        if len(branches) > int(state.get("limits", {}).get("branch_budget", 0)):
            errors.append("branch count exceeds branch budget")
        synthesis_doc = state.get("synthesis")
        if isinstance(synthesis_doc, Mapping):
            if synthesis_doc.get("selected_branch_id") not in branch_ids:
                errors.append("synthesis selected branch is missing")
            if synthesis_doc.get("fallback_branch_id") not in branch_ids:
                errors.append("synthesis fallback branch is missing")
            if not synthesis_doc.get("surviving_caveat"):
                errors.append("synthesis has no surviving caveat")
        generated_run = _run_export(state, branches, critiques)
        generated_graph = _graph_export(nodes, edges)
        for path, expected, label in (
            (directory / RUN_EXPORT, generated_run, "run export"),
            (directory / GRAPH_EXPORT, generated_graph, "graph export"),
        ):
            if not path.is_file():
                warnings.append(f"{label} is missing; run report/export to regenerate")
            else:
                try:
                    actual = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    errors.append(f"invalid {label}: {exc}")
                else:
                    if actual != expected:
                        errors.append(f"{label} does not match SQLite authority")
        expected_report = render_report_documents(state, branches, nodes, edges, critiques)
        report_path = directory / REPORT_FILE
        if not report_path.is_file():
            warnings.append("report.md is missing")
        elif report_path.read_text(encoding="utf-8") != expected_report:
            errors.append("report.md does not match SQLite authority")
        return {"ok": not errors, "errors": errors, "warnings": warnings, "checks": checks}
    except (sqlite3.Error, json.JSONDecodeError, ValidationError) as exc:
        errors.append(str(exc))
        return {"ok": False, "errors": errors, "warnings": warnings, "checks": checks}
    finally:
        conn.close()


def _run_export(
    state: Mapping[str, Any], branches: Sequence[Mapping[str, Any]], critiques: Sequence[Mapping[str, Any]]
) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "state": copy.deepcopy(dict(state)),
        "branches": [copy.deepcopy(dict(item)) for item in branches],
        "critiques": [copy.deepcopy(dict(item)) for item in critiques],
    }


def _graph_export(nodes: Sequence[Mapping[str, Any]], edges: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "nodes": [copy.deepcopy(dict(item)) for item in nodes],
        "edges": [copy.deepcopy(dict(item)) for item in edges],
    }


def sync_exports(
    project: Path | str,
    run_id: str,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> Tuple[Path, Path, Path]:
    directory = run_directory(project, run_id)
    own_connection = conn is None
    if conn is None:
        _directory, conn = open_run(project, run_id)
    assert conn is not None
    try:
        with _export_lock(directory):
            state = _load_run_state(conn)
            branches = list_branches(conn)
            nodes = list_nodes(conn)
            edges = list_edges(conn)
            critiques = list_critiques(conn)
            run_path = directory / RUN_EXPORT
            graph_path = directory / GRAPH_EXPORT
            report_path = directory / REPORT_FILE
            atomic_write_json(run_path, _run_export(state, branches, critiques))
            atomic_write_json(graph_path, _graph_export(nodes, edges))
            atomic_write_text(report_path, render_report_documents(state, branches, nodes, edges, critiques))
            return run_path, graph_path, report_path
    finally:
        if own_connection:
            conn.close()


def render_report_documents(
    state: Mapping[str, Any],
    branches: Sequence[Mapping[str, Any]],
    nodes: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
    critiques: Sequence[Mapping[str, Any]],
) -> str:
    brief = state["brief"]
    lines: List[str] = [
        f"# Deliberation report: {state['run_id']}",
        "",
        f"- Mode: `{state['mode']}`",
        f"- Phase: `{state['phase']}`",
        f"- Run revision: `{state['revision']}`",
        "",
        "## Problem",
        "",
        brief["problem"],
        "",
        "## Hard constraints",
        "",
    ]
    lines.extend(f"- {item}" for item in brief["hard_constraints"])
    lines.extend(["", "## Success criteria", ""])
    lines.extend(f"- {item}" for item in brief["success_criteria"])
    lines.extend(["", "## Execution cursor", ""])
    cursor = state["cursor"]
    lines.extend(
        [
            f"- Last completed: {cursor['last_completed']}",
            f"- In progress: {cursor['in_progress']}",
            f"- Next action: {cursor['next_action']}",
            "- Blockers: " + ("; ".join(cursor["blockers"]) if cursor["blockers"] else "none recorded"),
            "",
            "## Branches",
            "",
            "| Branch | Status | Strategy | Distinguishing assumption | Score |",
            "|---|---|---|---|---:|",
        ]
    )
    for branch in branches:
        score = "" if branch.get("weighted_score") is None else f"{float(branch['weighted_score']):.4f}"
        lines.append(
            "| {id} | {status} | {strategy} | {assumption} | {score} |".format(
                id=branch["branch_id"],
                status=branch["status"],
                strategy=str(branch["strategy"]).replace("|", "\\|"),
                assumption=str(branch["distinguishing_assumption"]).replace("|", "\\|"),
                score=score,
            )
        )
        if branch.get("summary"):
            lines.extend(["", f"### {branch['branch_id']}", "", branch["summary"]])
            lines.append("")
            lines.append("Assumptions:")
            lines.extend(f"- {item}" for item in branch.get("assumptions", []))
            lines.append("")
            lines.append("Risks:")
            lines.extend(f"- {item}" for item in branch.get("risks", []))
            if branch.get("remaining_weakness"):
                lines.extend(["", f"Remaining weakness: {branch['remaining_weakness']}"])
    evidence_nodes = [item for item in nodes if item.get("node_type") in {"evidence", "experiment"}]
    lines.extend(["", "## Evidence and experiments", ""])
    if evidence_nodes:
        for node in evidence_nodes:
            refs = ", ".join(node.get("source_refs", [])) or "none"
            lines.append(
                f"- `{node['node_id']}` [{node['status']}] {node['summary']} (branch: {node.get('branch_id') or 'shared'}; sources: {refs})"
            )
    else:
        lines.append("- None recorded.")
    lines.extend(["", "## Critiques", ""])
    if critiques:
        for critique in critiques:
            lines.append(
                f"- `{critique['critique_id']}` → `{critique['branch_id']}` "
                f"[{critique['severity']}/{critique['status']}] {critique['attack_type']}: {critique['summary']}"
            )
            if critique.get("response"):
                lines.append(f"  - Response: {critique['response']}")
            if critique.get("remaining_weakness"):
                lines.append(f"  - Remaining weakness: {critique['remaining_weakness']}")
    else:
        lines.append("- None recorded.")
    lines.extend(["", "## Synthesis", ""])
    synthesis_doc = state.get("synthesis")
    if isinstance(synthesis_doc, Mapping):
        lines.extend(
            [
                f"- Selected: `{synthesis_doc['selected_branch_id']}`",
                f"- Fallback: `{synthesis_doc['fallback_branch_id']}`",
                f"- Rationale: {synthesis_doc['rationale']}",
                "- Residual assumptions:",
            ]
        )
        lines.extend(f"  - {item}" for item in synthesis_doc["residual_assumptions"])
        lines.append("- Evidence that would reverse the decision:")
        lines.extend(f"  - {item}" for item in synthesis_doc["reversal_evidence"])
        lines.append("- Rejected branches:")
        lines.extend(f"  - `{key}`: {value}" for key, value in synthesis_doc["rejected_reasons"].items())
        lines.append(f"- Surviving caveat: {synthesis_doc['surviving_caveat']}")
    else:
        lines.append("- Not yet recorded.")
    lines.extend(
        [
            "",
            "## Graph summary",
            "",
            f"- Nodes: {len(nodes)}",
            f"- Edges: {len(edges)}",
            "",
            "> This report contains externally stated decision records. It does not expose or claim to preserve hidden chain-of-thought.",
            "",
        ]
    )
    return "\n".join(lines)


def inspect_run(project: Path | str, run_id: str) -> Dict[str, Any]:
    _directory, conn = open_run(project, run_id)
    try:
        return {
            "state": _load_run_state(conn),
            "branches": list_branches(conn),
            "nodes": list_nodes(conn),
            "edges": list_edges(conn),
            "critiques": list_critiques(conn),
        }
    finally:
        conn.close()


def context_ledger_fragment(
    project: Path | str,
    *,
    run_id: str,
    source_event_id: str,
    clear: bool = False,
) -> Dict[str, Any]:
    if not re.fullmatch(r"evt-[0-9]{12}", source_event_id):
        raise ValidationError("source_event_id must match evt-NNNNNNNNNNNN")
    state = get_run_state(project, run_id)
    relative = (RUNS_DIR / run_id).as_posix()
    if clear:
        operation = {"op": "clear_active_deliberation", "data": {}}
    else:
        _directory, conn = open_run(project, run_id)
        try:
            active_branches = [
                branch["branch_id"]
                for branch in list_branches(conn)
                if branch["status"] not in {"rejected", "deferred"}
            ]
        finally:
            conn.close()
        operation = {
            "op": "set_active_deliberation",
            "data": {
                "run_id": run_id,
                "path": relative,
                "phase": state["phase"],
                "status": "complete" if state["phase"] == "complete" else "active",
                "active_branches": active_branches,
                "next_action": state["cursor"]["next_action"],
            },
        }
    return {
        "source_event_ids": [source_event_id],
        "operations": [operation],
        "integration_note": (
            "Merge this fragment into the turn's complete context-ledger delta. Do not declare the source fully classified "
            "until every category in the user input is represented. This command does not execute context-ledger."
        ),
    }
