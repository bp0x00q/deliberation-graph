# Build receipt

Regression suite: **23/23 passed** in the final source-tree run.

## Verified by the bundled regression suite

- quick/deep/exhaustive mode defaults and normalized score weights;
- branch-budget and duplicate-distinguishing-axis rejection;
- minimum branch count and independent-first-pass phase gates;
- rejection of fields requesting hidden chain-of-thought or private scratchpads;
- evidence provenance requirements;
- graph endpoint integrity and dependency-cycle rejection;
- hard-gate-before-score ordering and deterministic maximize/minimize scoring;
- evidence-qualified shortlist requirements;
- cross-critique, independent critic, response, and revision gates;
- convergent branch creation with parent edges;
- synthesis requirements for selected/fallback branches, reversal evidence, complete rejection reasons, and a surviving caveat;
- deterministic report/export regeneration and complete-run validation;
- nonexecuting context-ledger fragment output;
- durable execution cursor;
- multiprocess concurrent node insertion with unique IDs and consistent exports;
- optimistic branch revision conflicts;
- clean malformed-input failures, JSON schemas, and package manifest.

## Boundaries and caveats

- The scripts manage externalized state; they do not perform the analytical work or call a model API.
- The skill does not preserve or expose hidden chain-of-thought.
- Branch diversity checks exact normalized distinguishing axes; semantically redundant wording may still require reviewer judgment.
- Scores depend on the supplied gates, dimensions, and evidence. They are not an objective truth function.
- SQLite serializes writes. Independent branch records reduce conflicts but do not create parallel model execution.
- A complete report can still be wrong when its assumptions, evidence, or branch set are weak.
- No claim is made that this skill executes untrusted code safely.
