"""Run the core/ tree search (MCTS + verifier + calibrated judge) on real tasks via TaskWorld.

    source env_mcts.sh
    python3 scripts/run_core_mcts.py --task-root tasks_v1 --tasks G_spi_02 --workers 1 --rounds 6
    python3 scripts/run_core_mcts.py --task-root tasks_v1 --tasks-file results/linear_failed.txt --workers 19
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.calibrated_judge import CalibratedJudge, _load_key  # noqa: E402
from core.mcts_engine import MCTS  # noqa: E402
from core.task_world import ForcedRipple, TaskWorld  # noqa: E402
from eda.grade import grade  # noqa: E402

LOCK = threading.Lock()


def _all_nodes(root):
    out, stack = [], [root]
    while stack:
        n = stack.pop()
        out.append(n)
        stack.extend(n.children)
    return out


def _path_tools(n):
    return [s.action.tool for s in n.path() if s.action]


def run_task(task_dir: Path, out_dir: Path, a):
    task = task_dir.name
    t0 = time.time()
    lines = []

    def log(s):
        line = f"[{task} {time.time() - t0:6.0f}s] {s}"
        lines.append(line)
        with LOCK:
            print(line, flush=True)

    ripple = ForcedRipple(_load_key())
    judge = CalibratedJudge(votes=2)
    world = TaskWorld(task_dir, ripple, judge, rollout_steps=a.rollout_steps, log=log)
    try:
        before = grade(task_dir, world.root_ws.root)
        log(f"hidden grade of start: reward={before['reward']} solved={before['solved']}")
        search = MCTS(world, world.constraints, k=a.k, rounds=a.rounds)
        res = search.run()
        nodes = [n for n in _all_nodes(res.root) if n.action is not None]
        submitted = [n for n in nodes if n.status != "PRUNED" and
                     (world.branch_truth(n).get("submitted") or getattr(n, "rollout_truth", {}).get("submitted"))]
        pool = submitted or [n for n in nodes if n.status != "PRUNED"] or nodes
        best = max(pool, key=lambda n: (n.blended_mean(), n.visits)) if pool else None
        chosen_from = "submitted" if submitted else ("alive" if pool else "none")
        if best is None:
            ws = world.root_ws
        else:
            ws = getattr(best, "rollout_ws", None) or world.ws_of(best)
        after = grade(task_dir, ws.root)
        edited = ws.digest() != world.root_ws.digest()
        # Keep the chosen design so PPA can be re-measured against the linear arm's final RTL.
        final_files = {}
        for p in sorted(Path(ws.root).rglob("*")):
            rel = p.relative_to(ws.root)
            if p.is_file() and not any(x.startswith(("_run", "_witness", ".")) for x in rel.parts):
                txt = p.read_text(errors="ignore")
                final_files[str(rel)] = txt if len(txt) <= 20000 else txt[:20000] + "\n...[truncated]"
        tree = [{"id": n.node_id, "parent": n.parent.node_id if n.parent else None, "depth": n.depth,
                 "tool": n.action.tool if n.action else None,
                 "args": {k: (v if len(str(v)) < 120 else str(v)[:120] + "...") for k, v in (n.action.args if n.action else {}).items()},
                 "status": n.status, "kill": n.kill_reason, "prior": round(n.prior, 3), "visits": n.visits,
                 "blend": round(n.blended_mean(), 3), "truth": n.result.truth if n.result else {},
                 "claim": n.claim.fields if n.claim else {},
                 "rollout_truth": getattr(n, "rollout_truth", None)} for n in _all_nodes(res.root)]
        out = {"task": task, "solved": after["solved"], "reward": after["reward"], "reward_start": before["reward"],
               "edited": edited, "chosen_from": chosen_from, "chosen_node": best.node_id if best else None,
               "chosen_path": _path_tools(best) if best else [],
               "failing_checks": [s["check"] for s in after["steps"] if not s["ok"]],
               "grade_steps": [{"check": s["check"], "ok": s["ok"], "score": s["score"]} for s in after["steps"]],
               "calls": ripple.calls + judge.calls, "worker_calls": ripple.calls, "judge_calls": judge.calls,
               "usd": round(ripple.usd + judge.usd, 4), "nodes": len(nodes),
               "baseline_truth": world.baseline, "constraints": {k: v for k, v in world.constraints.items() if k != "baseline"},
               "trace": [f"r{t.round_index} {t.phase} {t.node_id}: {t.detail}" for t in res.trace],
               "tree": tree, "wall_seconds": round(time.time() - t0, 1), "log": lines[-200:],
               "final_files": final_files,
               "grade_metrics": [{"check": s["check"], "metrics": {k: v for k, v in (s.get("metrics") or {}).items()
                                                                   if k not in ("cells_by_type", "by_code")}}
                                 for s in after["steps"]]}
        (out_dir / f"{task}.json").write_text(json.dumps(out, indent=1, default=str))
        log(f"DONE solved={out['solved']} reward={out['reward']} (start {before['reward']}) edited={edited} "
            f"calls={out['calls']} usd={out['usd']} {out['wall_seconds']}s")
        return out
    finally:
        world.cleanup()


def write_summary(out_dir: Path):
    rows = []
    for f in sorted(out_dir.glob("*.json")):
        try:
            rows.append(json.loads(f.read_text()))
        except Exception:
            pass
    n = len(rows)
    solved = sum(1 for r in rows if r["solved"])
    mean = sum(r["reward"] for r in rows) / n if n else 0.0
    lines = [f"solved {solved}/{n}  mean reward {mean:.4f}  total usd {sum(r.get('usd', 0) for r in rows):.4f}"]
    for r in rows:
        lines.append(f"{r['task']:<16} solved={r['solved']!s:<5} reward={r['reward']:<7} start={r.get('reward_start', '?'):<7} "
                     f"edited={r.get('edited')!s:<5} calls={r['calls']:<4} nodes={r['nodes']:<3} {r['wall_seconds']}s "
                     f"path={'>'.join(r.get('chosen_path', []))} fail={','.join(r.get('failing_checks', []))}")
    errs = sorted(out_dir.glob("*.error.txt"))
    if errs:
        lines.append("errors: " + ", ".join(e.name.replace(".error.txt", "") for e in errs))
    (out_dir / "summary.txt").write_text("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-root", default="tasks_v1")
    ap.add_argument("--tasks", nargs="*", default=[])
    ap.add_argument("--tasks-file")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--rounds", type=int, default=12)
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--rollout-steps", type=int, default=4)
    ap.add_argument("--out", default="results/core_mcts")
    a = ap.parse_args()

    tasks = list(a.tasks)
    if a.tasks_file:
        tasks += [l.strip() for l in Path(a.tasks_file).read_text().splitlines() if l.strip() and not l.startswith("#")]
    root = Path(a.task_root)
    out_dir = Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    todo = []
    for t in tasks:
        if (out_dir / f"{t}.json").exists():
            print(f"skip {t}: already done")
            continue
        if not (root / t / "task.yaml").exists():
            print(f"skip {t}: no task.yaml under {root / t}")
            continue
        todo.append(t)
    print(f"running {len(todo)} tasks with {a.workers} workers -> {out_dir}")

    def one(t):
        try:
            run_task(root / t, out_dir, a)
        except Exception:
            (out_dir / f"{t}.error.txt").write_text(traceback.format_exc())
            print(f"[{t}] ERROR\n{traceback.format_exc()}", flush=True)
        write_summary(out_dir)

    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        list(ex.map(one, todo))
    write_summary(out_dir)
    print((out_dir / "summary.txt").read_text())


if __name__ == "__main__":
    main()
