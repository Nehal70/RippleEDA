"""Join tree-search results against the linear Ripple baseline (STATUS.md, 50 tasks, 30 calls, one seed).

    python3 scripts/compare_arms.py results/core_mcts          # or results/mcts_eval
    python3 scripts/compare_arms.py results/core_mcts --md     # markdown table for the slide

Also scans each tree run's tool log for run_shell commands that touch solution/, tb/, tasks/ or other
workspaces and marks those tasks as CONTAMINATED so they can be excluded honestly.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Linear Ripple baseline, hidden grader, one run per task, 30 calls. Final: 26/50 solved, 24 failed.
LINEAR = {
    "G_afifo_01": (False, 0.498, "sim,synth,sta"), "G_afifo_02": (False, 0.784, "sim"),
    "G_afifo_03": (False, 0.784, "sim"), "G_afifo_04": (False, 0.431, "sim"),
    "G_afifo_05": (False, 0.430, "sim"), "G_alu_01": (False, 0.922, "sim"),
    "G_alu_07": (False, 0.996, "sta"), "G_alu_08": (False, 0.862, "lint,sim"),
    "G_div_01": (False, 0.286, "sim,sta"), "G_div_02": (False, 0.655, "sim,sta"),
    "G_div_04": (False, 0.994, "sta"), "G_div_05": (False, 0.710, "sim,sta"),
    "G_div_06": (False, 0.715, "sim"), "G_mac_01": (False, 0.992, "sta"),
    "G_mac_02": (False, 0.992, "sta"), "G_mac_03": (False, 0.499, "sim"),
    "G_mac_04": (False, 0.992, "sta"), "G_spi_01": (False, 0.730, "sim"),
    "G_spi_02": (False, 0.762, "sim,sta"), "G_spi_03": (False, 0.748, "sim"),
    "G_spi_04": (False, 0.739, "sim"), "G_spi_05": (False, 0.880, "sim"),
    "G_spi_06": (False, 0.756, "sim"), "G_uart_05": (False, 0.000, "lint,sim,synth,sta"),
}
CONTROLS = ["G_fifo_03", "G_irq_03", "G_uart_02", "G_alu_04", "G_mac_05", "G_div_03"]
for c in CONTROLS:
    LINEAR[c] = (True, 1.0, "-")

LEAK = re.compile(r"solution|/tb/|\btb/|tasks/|tasks_v1|/tmp/ws_|find /|grep -r", re.I)


def leak_check(rec: dict) -> list[str]:
    hits = []
    blobs = []
    for key in ("log", "trace", "tree", "messages"):
        v = rec.get(key)
        if v:
            blobs.append(json.dumps(v, default=str))
    text = "\n".join(blobs)
    for m in re.finditer(r"run_shell[^\n]{0,200}", text):
        if LEAK.search(m.group(0)):
            hits.append(m.group(0)[:160])
    return hits


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    out_dir = Path(sys.argv[1])
    md = "--md" in sys.argv
    rows = []
    for f in sorted(out_dir.glob("G_*.json")):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        name = rec["task"]
        fin = rec["final"]
        fails = ",".join(s["check"] for s in fin.get("steps", []) if not s.get("ok")) or "-"
        leaks = leak_check(rec)
        rows.append((name, fin["solved"], fin["reward"], fails, rec.get("calls"), rec.get("wall_seconds", rec.get("seconds")),
                     (rec.get("usage") or {}).get("usd"), leaks))

    failed_set = [r for r in rows if r[0] in LINEAR and not LINEAR[r[0]][0]]
    ctrl_set = [r for r in rows if r[0] in CONTROLS]
    clean = [r for r in rows if not r[7]]

    def summ(rs, label):
        if not rs:
            return f"{label}: none finished"
        n = len(rs)
        solved = sum(1 for r in rs if r[1])
        tree_r = sum(r[2] for r in rs) / n
        lin_r = sum(LINEAR[r[0]][1] for r in rs) / n
        better = sum(1 for r in rs if r[2] > LINEAR[r[0]][1] + 1e-6)
        worse = sum(1 for r in rs if r[2] < LINEAR[r[0]][1] - 1e-6)
        return (f"{label}: n={n}  tree solved {solved}/{n}  linear solved {sum(1 for r in rs if LINEAR[r[0]][0])}/{n}  "
                f"mean reward tree {tree_r:.3f} vs linear {lin_r:.3f}  tree better on {better}, worse on {worse}")

    print(summ(failed_set, "linear-failed tasks"))
    print(summ(ctrl_set, "controls (linear-solved)"))
    print(summ(clean, "all finished, leak-free"))
    contaminated = [r[0] for r in rows if r[7]]
    if contaminated:
        print("CONTAMINATED (run_shell touched hidden/other files):", ", ".join(contaminated))
    print()
    if md:
        print("| task | linear solved | linear reward | tree solved | tree reward | tree fails | calls | min | $ |")
        print("|---|---|---|---|---|---|---|---|---|")
        for r in rows:
            ls, lr, _ = LINEAR.get(r[0], (None, float("nan"), ""))
            flag = " (LEAK)" if r[7] else ""
            print(f"| {r[0]}{flag} | {'yes' if ls else 'no'} | {lr:.3f} | {'yes' if r[1] else 'no'} | {r[2]:.3f} | {r[3]} | "
                  f"{r[4]} | {(r[5] or 0) / 60:.0f} | {r[6] if r[6] is None else f'{r[6]:.3f}'} |")
    else:
        print(f"{'task':<12} {'lin':>4} {'lin_r':>6} {'tree':>5} {'tree_r':>7} {'tree fails':<16} {'calls':>5} {'min':>4} leak")
        for r in rows:
            ls, lr, _ = LINEAR.get(r[0], (None, float("nan"), ""))
            print(f"{r[0]:<12} {'yes' if ls else 'no':>4} {lr:>6.3f} {'yes' if r[1] else 'no':>5} {r[2]:>7.3f} {r[3]:<16} "
                  f"{str(r[4]):>5} {(r[5] or 0) / 60:>4.0f} {'YES' if r[7] else ''}")
    not_run = [t for t in LINEAR if t not in {r[0] for r in rows}]
    if not_run:
        print(f"\nnot finished ({len(not_run)}): {', '.join(not_run)}")


if __name__ == "__main__":
    main()
