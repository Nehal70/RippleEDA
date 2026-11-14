"""
Export exactly what the agent (Ripple, linear or MCTS) is allowed to see for a task:
the prompt and a fresh copy of start/. Nothing from task.yaml's checks / why_mcts,
and nothing from tb/, formal/, ref/, mutants/ or solution/.

    python -m eda.agent_view tasks/T05_xprop_reset_pipeline /tmp/run1/T05
    -> /tmp/run1/T05/PROMPT.md + /tmp/run1/T05/rtl/... (the editable workspace)

In code:  prompt, ws = export(task_dir, out_dir)
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import yaml


def export(task_dir, out_dir):
    task_dir, out_dir = Path(task_dir), Path(out_dir)
    spec = yaml.safe_load((task_dir / "task.yaml").read_text())
    if out_dir.exists():
        shutil.rmtree(out_dir)
    shutil.copytree(task_dir / "start", out_dir)
    prompt = spec["prompt"].strip()
    (out_dir / "PROMPT.md").write_text(f"# Task {spec['id']}\n\n{prompt}\n")
    return prompt, out_dir


if __name__ == "__main__":
    p, ws = export(sys.argv[1], sys.argv[2])
    print(f"workspace: {ws}\n\n{p}")
