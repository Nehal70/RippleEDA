"""Run a real search once OPENROUTER_API_KEY is in .env. The key is never printed."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.judge import JUDGE_INSTRUCTIONS
from core.live_world import LiveChipWorld
from core.mcts_engine import MCTS
from core.openrouter_client import DEFAULT_MODEL, OpenRouterClient, RippleActor, load_env_file

DESIGN = ROOT / "examples" / "counter.v"
TASK = (
    "The counter in examples/counter.v may be fine, or it may be wrong. "
    "Check it. Do not run a full place-and-route. "
    "Keep the cell count at or under 100 and do not add latches."
)
CONSTRAINTS = {"forbid_latches": True, "max_cells": 100, "max_cycles": 4}


def main() -> None:
    load_env_file()
    key = os.environ.get("OPENROUTER_API_KEY", "")
    model = os.environ.get("RIPPLE_MODEL") or DEFAULT_MODEL
    if model.startswith("your_"):
        model = DEFAULT_MODEL
    if not key or key.startswith("your_"):
        print("No live call made.")
        print("Put your key in .env as OPENROUTER_API_KEY=... then run this again.")
        print(f"Model: {model}")
        print("Judge rules are in core/judge.py")
        print("Until then: python3 scripts/run_example_rollout.py")
        return

    print(f"Key found. Model: {model}")
    print("Judge file: core/judge.py")
    print("This run is small on purpose: 2 rounds, 3 ideas each, 2 steps per play-out.")
    print()
    client = OpenRouterClient(model)
    world = LiveChipWorld(
        RippleActor(client),
        task=TASK,
        design_path=str(DESIGN),
        constraints=CONSTRAINTS,
        rollout_steps=2,
    )
    result = MCTS(world, constraints=CONSTRAINTS, k=3, rounds=2).run()
    for step in result.trace:
        print(f"Round {step.round_index}  {step.phase:8}  {step.node_id:6}  {step.detail}")
    best = result.best
    if best and best.action:
        print()
        print(f"Chosen first move: {best.action.tool}")
        print(f"Blend {best.blended_mean():.2f}")
    print()
    print("--- judge instructions ---")
    print(JUDGE_INSTRUCTIONS)


if __name__ == "__main__":
    main()
