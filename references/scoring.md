# Scoring

Hard gates run before scoring. A branch that violates a non-negotiable constraint is excluded rather than compensated by strengths elsewhere.

Every scoring dimension uses a value from 0 to 1 and a positive normalized weight. `maximize` treats larger as better. `minimize` converts the raw value to `1 - value` before weighting.

Use task-specific dimensions. Common examples:

- correctness;
- evidence quality;
- maintainability;
- implementation cost;
- operational burden;
- reversibility;
- migration risk;
- uncertainty.

Scores support comparison; they do not select the winner automatically. The synthesis must still explain why the chosen branch won, what could reverse the decision, and which fallback remains viable.
