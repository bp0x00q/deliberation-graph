# deliberation-graph

A Python, standard-library-only Agent Skill for durable multi-branch analysis. It stores explicit approaches, evidence, critiques, scores, and decisions in a transactional project-local run. The host agent performs reasoning; the package does not call a model API or request private chain-of-thought.

Run the regression suite:

```bash
python3 scripts/self_test.py
```

Create a run from `assets/brief.template.json`, then use `scripts/deliberation.py --help` for lifecycle commands.
