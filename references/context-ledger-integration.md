# Context Ledger integration

`deliberation-graph` owns the complete branch graph. `context-ledger` needs only a bounded pointer:

- run ID and path;
- current phase;
- active branch IDs;
- literal next action;
- active/complete status.

Generate an operation fragment:

```bash
python3 scripts/deliberation.py --project . context-ledger-fragment \
  --run-id example-run \
  --source-event-id evt-000000000042
```

The command prints JSON and does not invoke context-ledger. Merge the operation into the full turn delta. The fragment omits source disposition because one user input may also contain directives, preferences, task changes, or commitments that this skill cannot classify.

Use `--clear` only when intentionally detaching the run. A completed run may remain linked with status `complete` when its decision still matters.
