---
name: deliberation-graph
description: >-
  Use structured multi-branch deliberation for complex, ambiguous, high-impact,
  or failure-prone problems. Create independent solution branches, causal
  hypotheses, assumptions, evidence, experiments, critiques, and decision
  criteria; prune invalid branches, merge compatible discoveries, red-team
  leading candidates, and produce a traceable synthesis. Preserve active branch
  state across interruption or compaction. Skip simple deterministic tasks.
compatibility: Requires Python 3.10+ for the bundled deterministic state manager. No network service or model API is required.
metadata:
  version: "1.0.0"
  self_test: "python3 scripts/self_test.py"
---

# Deliberation Graph

Use this skill when the cost of prematurely locking onto one approach is higher than the cost of exploring several explicit alternatives.

The host agent performs the analysis. The bundled Python scripts only manage durable, inspectable state. They do not call a model, execute captured content, or request hidden chain-of-thought.

## Hard boundaries

This skill does **not**:

- expose, preserve, or require private chain-of-thought;
- claim that more branches automatically produce a better answer;
- turn persona variation into analytical diversity;
- authorize or sandbox execution of untrusted code;
- treat a numerical score as a substitute for hard constraints or judgment;
- call a model API or silently run experiments.

Record only decision-relevant externalized material: approaches, assumptions, evidence, experiments, risks, critiques, conclusions, uncertainty, and next actions.

## When to activate

Use it for architecture, difficult debugging with competing causal hypotheses, consequential planning, migration strategy, security design, research synthesis, or any problem where several materially different approaches remain plausible.

Do not use it for a one-command lookup, a mechanical edit, a straightforward calculation, or a task with one already-proven implementation path.

## Storage model

Each run is project-local:

```text
.deliberation/runs/<run-id>/
├── run.sqlite3   # transactional authority
├── run.json      # state/branch/critique export
├── graph.json    # node/edge export
└── report.md     # deterministic human-readable report
```

SQLite serializes commits. Branch revisions allow independent branch work without one global last-writer-wins update. JSON and Markdown files are derived exports and can be regenerated.

## Deliberation lifecycle

```text
framing
  → branching
  → independent-exploration
  → evidence
  → cross-critique
  → revision
  → synthesis
  → complete
```

Transitions move one phase at a time and enforce phase-specific invariants.

### 1. Frame the decision

Start from a brief containing:

- the problem and relevant visible context;
- hard constraints and observable success criteria;
- unresolved unknowns;
- hard gates that cannot be traded away;
- weighted scoring dimensions, each marked `maximize` or `minimize`.

```bash
python3 scripts/deliberation.py --project . create \
  --run-id storage-architecture \
  --mode deep \
  --brief-file brief.json
```

Use `assets/brief.template.json` as a starting point.

### 2. Generate genuinely distinct branches

Move to `branching`, then add branches whose distinguishing axes are explicit:

```bash
python3 scripts/deliberation.py --project . phase \
  --run-id storage-architecture \
  --target branching \
  --expected-run-revision 0

python3 scripts/deliberation.py --project . add-branch \
  --run-id storage-architecture \
  --strategy "SQLite transactional authority" \
  --distinguishing-assumption "A local SQLite runtime is available" \
  --optimization-target "consistency under concurrent writers" \
  --known-tradeoff "single-file database dependency"
```

Do not create branches that differ only in tone or persona. Useful axes include simplest viable, highest robustness, lowest operations burden, existing-system adaptation, clean-slate design, contrarian/no-build, failure-first, or a genuinely different causal hypothesis.

Modes set default budgets:

- `quick`: 3 branches, shortlist 2;
- `deep`: at least 5 branches, budget 7, shortlist 3;
- `exhaustive`: at least 5 branches, budget 12, shortlist 4.

Budgets control work, not answer length.

### 3. Explore independently

After the minimum branch count is reached, transition to `independent-exploration`. Give every branch the same problem brief and shared evidence, but do not reveal other branches' conclusions during its first pass.

For each branch record:

- a concise approach summary;
- explicit assumptions;
- concrete failure risks;
- tests or observations that could falsify it.

```bash
python3 scripts/deliberation.py --project . first-pass \
  --run-id storage-architecture \
  --branch-id branch-001 \
  --expected-branch-revision 2 \
  --summary "Use SQLite transactions for authoritative state." \
  --assumption "Writers share a local filesystem" \
  --risk "Lock contention under bursty parallel work" \
  --next-test "Run a multiprocess append fixture"
```

The run cannot enter evidence evaluation while an active branch lacks a first pass.

### 4. Attach evidence and test hard gates

Record observed evidence only with a source reference. Use `unverified` when a claim lacks direct support.

```bash
python3 scripts/deliberation.py --project . add-node \
  --run-id storage-architecture \
  --type evidence \
  --branch-id branch-001 \
  --title "Contention fixture" \
  --summary "Four writers preserved unique contiguous IDs." \
  --status observed \
  --source-ref "test:multiprocess-append"
```

Evaluate **every** configured hard gate before scoring. A failed hard gate excludes the branch from scoring and selection.

Scoring is a comparison aid. It cannot repair missing evidence or a violated hard constraint.

### 5. Shortlist and cross-critique

Shortlist at least two evidence-qualified, scored branches. During `cross-critique`, challenge each shortlisted branch with a concrete attack:

- constraint violation;
- hidden dependency or incorrect assumption;
- operational, security, scaling, or migration failure;
- counterexample or simpler alternative;
- weak or circular evidence.

A branch cannot be its own critic. Use `independent` when the critic is not another branch.

```bash
python3 scripts/deliberation.py --project . critique \
  --run-id storage-architecture \
  --branch-id branch-001 \
  --critic-branch-id branch-002 \
  --attack-type scaling-failure \
  --severity high \
  --summary "The write path may serialize long transactions."
```

The critic attacks the branch; it does not silently replace the branch with its preferred solution.

### 6. Resolve critiques and revise

Every shortlisted branch must receive a critique. Before revision completes, each critique must be answered or accepted. Record the remaining weakness rather than pretending the critique vanished.

```bash
python3 scripts/deliberation.py --project . respond \
  --run-id storage-architecture \
  --critique-id critique-000001 \
  --expected-critique-revision 1 \
  --response "Transactions are short and the fixture covers concurrent appends." \
  --remaining-weakness "Network filesystems were not tested"
```

Compatible discoveries may converge through `merge`; the graph retains links to every parent branch.

### 7. Synthesize with a fallback

A final synthesis must include:

- the selected branch and a distinct fallback;
- why the selected branch won;
- residual assumptions;
- evidence that would reverse the decision;
- a reason for every non-selected branch;
- one surviving caveat.

The run remains in `synthesis` until this record exists, then may transition to `complete`.

## Graph vocabulary

Node types include `problem`, `constraint`, `question`, `hypothesis`, `approach`, `evidence`, `assumption`, `experiment`, `critique`, `risk`, `decision`, and `synthesis`.

Edges include `supports`, `contradicts`, `depends_on`, `supersedes`, `derived_from`, `tests`, `blocks`, `merges_with`, and `selected_over`. Dependency cycles are rejected; evidence and critique relationships may form richer non-tree structures.

## Resume and context compaction

Checkpoint the literal cursor whenever work stops:

```bash
python3 scripts/deliberation.py --project . checkpoint \
  --run-id storage-architecture \
  --expected-run-revision 18 \
  --last-completed "Scored three viable branches" \
  --in-progress "Selecting the cross-critique shortlist" \
  --next-action "Shortlist branch-001 and branch-003"
```

For `context-ledger`, emit—not execute—an operation fragment:

```bash
python3 scripts/deliberation.py --project . context-ledger-fragment \
  --run-id storage-architecture \
  --source-event-id evt-000000000042
```

Merge the fragment into the turn's complete context-ledger delta. The fragment intentionally omits `source_dispositions`; only the agent handling the full user input can declare its exact semantic categories.

## Validation

```bash
python3 scripts/deliberation.py --project . validate --run-id storage-architecture
python3 scripts/deliberation.py --project . report --run-id storage-architecture
```

Validation checks SQLite integrity, branch and graph references, dependency cycles, phase invariants, synthesis completeness, forbidden hidden-reasoning fields, and export consistency.
The domain and SQLite implementation is in `scripts/graph_core.py`; package maintainers refresh drift hashes with `scripts/write_manifest.py`.

## References

- `references/branching-strategies.md`
- `references/criticism-protocol.md`
- `references/evidence-policy.md`
- `references/scoring.md`
- `references/context-ledger-integration.md`
