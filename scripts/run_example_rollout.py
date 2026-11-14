"""Print one full search on the scripted chip task. No API key required."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.mcts_engine import MCTS
from examples.scripted_chip_world import CONSTRAINTS, ScriptedChipWorld


def main() -> None:
    result = MCTS(ScriptedChipWorld(), constraints=CONSTRAINTS, k=3, rounds=8).run()
    print("TASK")
    print("  The slave hangs when RREADY drops. Keep cells at or under 100.")
    print("  Ripple does not get told which tool to call.")
    print()
    for step in result.trace:
        print(f"Round {step.round_index}  {step.phase:8}  {step.node_id:6}  {step.detail}")
    print()
    best = result.best
    if best is None or best.action is None:
        print("No living branch.")
        return
    print(f"CHOSEN FIRST MOVE: {best.action.tool}")
    print(f"  blend {best.blended_mean():.2f}   visits {best.visits}   status {best.status}")
    print()
    print("TREE")
    _print(result.root, "")


def _print(node, indent: str) -> None:
    if node.action is None:
        label = "root"
    else:
        label = node.action.tool
    extra = ""
    if node.status == "PRUNED":
        extra = f"  KILLED ({node.kill_reason})"
    elif node.node_id != "root":
        extra = (
            f"  blend={node.blended_mean():.2f} judge={node.prior:.2f} "
            f"{'not played out' if node.visits == 0 else f'playouts={node.visits}'}"
        )
    print(f"{indent}{node.node_id} {label}{extra}")
    for child in node.children:
        _print(child, indent + "  ")


if __name__ == "__main__":
    main()
