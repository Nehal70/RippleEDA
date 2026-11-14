"""Same live search as scripts/run_live.py, with the calibrated judge and the judge-vs-rollout log.

    source env_mcts.sh && python scripts/run_live_calibrated.py
    python -m core.judge_log results/judge_log.jsonl      # accumulate runs, then fit prior_strength (lambda)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.calibrated_judge import CalibratedJudge, CalibratedRippleActor  # noqa: E402
from core.judge_log import JudgeLog, fit_prior_strength, judge_report  # noqa: E402
from core.live_world import LiveChipWorld  # noqa: E402
from core.mcts_engine import MCTS  # noqa: E402
from core.openrouter_client import OpenRouterClient, RippleActor, load_env_file  # noqa: E402

DESIGN = ROOT / "examples" / "counter.v"
TASK = ("The counter in examples/counter.v may be fine, or it may be wrong. Check it. Do not run a full "
        "place-and-route. Keep the cell count at or under 100 and do not add latches.")
CONSTRAINTS = {"forbid_latches": True, "max_cells": 100, "max_cycles": 4}


def main() -> None:
    load_env_file()
    judge = CalibratedJudge(votes=2)
    actor = CalibratedRippleActor(RippleActor(OpenRouterClient()), judge, TASK)
    world = LiveChipWorld(actor, task=TASK, design_path=str(DESIGN), constraints=CONSTRAINTS, rollout_steps=2)
    mcts = MCTS(world, constraints=CONSTRAINTS, k=3, rounds=2)
    log = JudgeLog().attach(mcts)
    result = mcts.run()
    for step in result.trace:
        print(f"Round {step.round_index}  {step.phase:8}  {step.node_id:6}  {step.detail}")
    log.save(ROOT / "results" / "judge_log.jsonl")
    print("\njudge vs rollout (this run):", judge_report(log.records))
    print("lambda fit (this run):", fit_prior_strength(log.records)["prior_strength"],
          "- accumulate several runs in results/judge_log.jsonl before trusting it")
    print(f"judge calls {judge.calls}, cost ${judge.usd:.4f}")


if __name__ == "__main__":
    main()
