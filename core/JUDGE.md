# Calibrated Ripple judge

`core/calibrated_judge.py` is a drop-in for `RippleActor.judge(history, raw) -> (score, evidence)`.
Its score is calibrated to the expected outcome score, so it sits on the same scale as the rollout
scores it is blended with in `Node.blended_mean()`.

## Design

| Research idea | Implementation |
|---|---|
| Think before scoring (MT-Bench, GenRM, ThinkPRM) | Fields in order: evidence, rationale, p_success, progress |
| Quote evidence, verify it in code (Agent-as-a-Judge) | Quote must occur in the tool log (`evidence_in_log`); also a calibration feature |
| Compare instead of rate | `compare(task, a, b)` and `rerank_near_ties(...)` for siblings within 0.05 |
| Swap the order (position bias) | Pairwise runs both orders; a win counts only if it wins both, else tie |
| Length / self-preference bias | Judge sees tool calls and logs only; worker rationale lines are stripped |
| Several samples (PoLL, self-consistency) | `votes=2` by default; mean and spread are calibration features |
| Anchored bands + reference | 0-0.2 / 0.3-0.5 / 0.6-0.8 / 0.9-1.0 bands; reference = per-task success criteria (generated once, cached) |
| Reward progress (PAV, AgentPRM) | `progress` in {-1, 0, +1} vs the parent step |
| Measure the judge, set lambda | `core/judge_log.py`: logs judge guess vs rollout, fits `prior_strength` |
| Never let the judge kill a branch | Unchanged: it only sets `Node.prior` |

Ripple-specific: Ripple only answers reliably through a forced tool call with built-in reasoning off.
In free text it spends its whole token budget on hidden reasoning and returns nothing (measured 56 of 64
empty answers). The same failure can hit `OpenRouterClient.complete()`.

## Calibration

`scripts/calibrate_judge.py` builds labelled design states (start, partial fixes, solution per task),
shows the judge exactly what a search node would see (task + real lint / smoke-sim / synth / STA output),
labels each with the hidden grader, fits a logistic map on half the tasks and tests on the other half.

| Held-out tasks (26 states) | AUROC (solved) | Brier (soft) | ECE (soft) | Ranking within task |
|---|---|---|---|---|
| Raw Ripple judge | 0.988 | 0.125 | 0.220 | 0.842 |
| Calibrated | 0.985 | 0.083 | 0.066 | 0.842 |

Calibrated to the expected score (soft label), not to P(fully solved): mostly-fixed states score
0.7-0.9. Fitted on 55 states; refit with the 50 generated tasks for a larger set. Weights are in
`core/judge_calibration.json`.

## Use

```python
from core.calibrated_judge import CalibratedJudge, CalibratedRippleActor
actor = CalibratedRippleActor(RippleActor(OpenRouterClient()), CalibratedJudge(votes=2), task)
world = LiveChipWorld(actor, task=task, design_path=..., constraints=...)
mcts = MCTS(world, ...); log = JudgeLog().attach(mcts); mcts.run(); log.save("results/judge_log.jsonl")
```

`python scripts/run_live_calibrated.py` runs this end to end. `python -m core.judge_log results/judge_log.jsonl`
reports judge-vs-rollout agreement and the fitted `prior_strength` (lambda).
