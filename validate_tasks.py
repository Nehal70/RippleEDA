"""Re-grade every task: start/ must NOT be solved, solution/ MUST be solved.
    python validate_tasks.py            # all tasks
    python validate_tasks.py T05 T11    # subset
"""
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from eda.grade import grade

ROOT = Path(__file__).parent / "tasks"


def one(task):
    s = grade(task, task / "start")
    r = grade(task, task / "solution")
    return task.name, s, r


if __name__ == "__main__":
    sel = sys.argv[1:]
    tasks = sorted(t for t in ROOT.iterdir() if (t / "task.yaml").exists() and (not sel or any(t.name.startswith(x) for x in sel)))
    t0 = time.time()
    ok_all = True
    print(f"{'task':<30} {'start':>14} {'solution':>14}  verdict")
    with ProcessPoolExecutor(max_workers=6) as ex:
        for name, s, r in ex.map(one, tasks):
            good = (not s["solved"]) and r["solved"]
            ok_all &= good
            print(f"{name:<30} {s['reward']:>6.3f} {'solved' if s['solved'] else 'FAIL':>7} "
                  f"{r['reward']:>6.3f} {'solved' if r['solved'] else 'FAIL':>7}  {'OK' if good else '<-- CHECK'}"
                  f"   ({s['seconds']:.1f}s / {r['seconds']:.1f}s)")
    print(f"\n{'ALL TASKS VALID' if ok_all else 'SOME TASKS NEED ATTENTION'}  ({time.time() - t0:.0f}s)")
    sys.exit(0 if ok_all else 1)
