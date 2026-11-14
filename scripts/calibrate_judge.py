"""Fit and evaluate the calibrated judge (core/calibrated_judge.py).

Dataset: every task's design states (start, partial fixes, solution). For each state we build exactly what the
search's judge sees at a node - the task text plus real tool output (lint, the task's shipped smoke test,
synthesis, timing) - and label it with the HIDDEN grader (soft reward and solved). The judge never sees labels.

Split by task (half fit, half test) so the reported calibration is out-of-sample.
Metrics: AUROC (solved), Brier, ECE (10 bins, vs soft reward), within-task pairwise ranking accuracy;
raw judge vs calibrated. Writes core/judge_calibration.json and results/judge_calibration_report.md.

    source env_mcts.sh && python scripts/calibrate_judge.py              # uses cached judge outputs if present
    python scripts/calibrate_judge.py --refresh --votes 3 --model ricursiveai/ripple-65b-preview
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.calibrated_judge import Calibrator, CalibratedJudge, Verdict  # noqa: E402
from eda import tools  # noqa: E402
from eda.grade import grade  # noqa: E402

CACHE = ROOT / "results" / "judge_calib_cache.json"


# ----------------------------------------------------------------------------- observations
def observe(task_dir: Path, state_dir: Path | None, name: str) -> dict:
    """Run the public tools on a design state and build (history, raw) like a search node would."""
    import tempfile
    import yaml
    from agent.workspace import Workspace
    spec = yaml.safe_load((task_dir / "task.yaml").read_text())
    ws = Workspace(task_dir)
    if state_dir is not None:
        shutil.copytree(state_dir, ws.root, dirs_exist_ok=True)
    rtl = ws.rtl()
    cwd = Path(tempfile.mkdtemp(prefix="calib_"))
    parts, history = [], [f"Task: {spec['prompt'].strip()}"]
    L = tools.lint(rtl, spec["top"], cwd=cwd)
    history.append(f"Called verilator_lint. Output: errors={L['metrics']['errors']} warnings={L['metrics']['warnings']}")
    if L["metrics"]["errors"]:
        parts.append("== verilator --lint-only -Wall\n" + "\n".join(L["messages"][:15]))
    else:
        for pc in spec.get("public_checks", []):
            if pc["type"] in ("sim", "gls"):
                fn = tools.sim_iverilog if pc["type"] == "sim" else None
                if fn is None:
                    r = tools.gls(rtl, spec["top"], [str(ws.orig / f) for f in pc["tb"]], pc.get("top", "tb"), cwd=cwd, params=pc.get("params"))
                else:
                    r = fn(rtl + [str(ws.orig / f) for f in pc["tb"]], pc.get("top", "tb"), cwd=cwd, params=pc.get("params"))
                parts.append(f"== simulate {' '.join(pc['tb'])} {pc.get('params') or ''}\n"
                             + "\n".join(r.get("first_errors", [])[:10]) + "\n" + r["log"][-800:])
            elif pc["type"] == "cocotb":
                r = tools.sim_cocotb(rtl, spec["top"], pc["module"], ws.orig / pc["dir"], cwd=cwd)
                parts.append(f"== cocotb {pc['module']}\n" + r["log"][-1200:])
        S = tools.synth(rtl, spec["top"], cwd=cwd, recipe=ws.recipe())
        if S["ok"]:
            m = S["metrics"]
            parts.append(f"== yosys synth (sky130hd)\narea={m['area']} cells={m['cells']} flops={m['flops']} "
                         f"latches={m['latches']} logic_loops={m['logic_loops']}")
            per = next((c["period_ns"] for c in spec["checks"] if c["type"] == "sta"), None)
            if per:
                T = tools.sta(S["netlist"], spec["top"], cwd=cwd, period_ns=per,
                              clock_port=next((c.get("clock", "clk") for c in spec["checks"] if c["type"] == "sta"), "clk"))
                parts.append(f"== opensta @ {per} ns\nWNS={T['metrics']['wns']} critical_path={T['metrics']['critical_path_ns']}")
        else:
            parts.append("== yosys synth FAILED\n" + S["log"][-600:])
    g = grade(task_dir, ws.root)
    ws.cleanup()
    shutil.rmtree(cwd, ignore_errors=True)
    return {"task": task_dir.name, "state": name, "prompt": spec["prompt"].strip(), "history": "\n".join(history),
            "raw": "\n\n".join(parts)[-9000:], "reward": g["reward"], "solved": g["solved"]}


def states_of(task_dir: Path):
    out = [(None, "start")]
    if (task_dir / "partial").exists():
        out += [(p, "partial:" + p.name) for p in sorted((task_dir / "partial").iterdir()) if p.is_dir()]
    out.append((task_dir / "solution", "solution"))
    return out


def _obs_job(args):
    td, sd, name = args
    try:
        return observe(Path(td), Path(sd) if sd else None, name)
    except Exception as e:
        return {"task": Path(td).name, "state": name, "error": repr(e)}


# ----------------------------------------------------------------------------- metrics
def auroc(scores, labels):
    pos = [s for s, y in zip(scores, labels) if y]
    neg = [s for s, y in zip(scores, labels) if not y]
    if not pos or not neg:
        return float("nan")
    return sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / (len(pos) * len(neg))


def brier(scores, labels):
    return sum((s - y) ** 2 for s, y in zip(scores, labels)) / max(1, len(scores))


def ece(scores, labels, bins=10):
    tot, n = 0.0, len(scores)
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        idx = [i for i, s in enumerate(scores) if (lo <= s < hi) or (b == bins - 1 and s == 1.0)]
        if idx:
            tot += len(idx) / n * abs(sum(scores[i] for i in idx) / len(idx) - sum(labels[i] for i in idx) / len(idx))
    return tot


def pairwise(rows, key):
    ok = tot = 0.0
    by = {}
    for r in rows:
        by.setdefault(r["task"], []).append(r)
    for rs in by.values():
        for i in range(len(rs)):
            for j in range(i + 1, len(rs)):
                a, b = rs[i], rs[j]
                if abs(a["reward"] - b["reward"]) < 1e-6:
                    continue
                tot += 1
                sa, sb = key(a), key(b)
                ok += 0.5 if abs(sa - sb) < 1e-9 else float((sa > sb) == (a["reward"] > b["reward"]))
    return ok / tot if tot else float("nan")


def report(name, rows, key):
    s = [key(r) for r in rows]
    return {"set": name, "n": len(rows), "auroc_solved": auroc(s, [r["solved"] for r in rows]),
            "brier_soft": brier(s, [r["reward"] for r in rows]), "brier_solved": brier(s, [float(r["solved"]) for r in rows]),
            "ece_soft": ece(s, [r["reward"] for r in rows]), "pairwise_within_task": pairwise(rows, key)}


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="re-run tools and judge (ignore cache)")
    ap.add_argument("--model", default=None)
    ap.add_argument("--votes", type=int, default=2)
    ap.add_argument("--tasks", default="all")
    a = ap.parse_args()
    (ROOT / "results").mkdir(exist_ok=True)
    cache = json.loads(CACHE.read_text()) if CACHE.exists() and not a.refresh else {}

    tasks = sorted(t for t in (ROOT / "tasks").iterdir() if (t / "task.yaml").exists() and (t / "solution").exists())
    if a.tasks != "all":
        tasks = [t for t in tasks if any(t.name.startswith(x) for x in a.tasks.split(","))]
    jobs = [(str(t), str(sd) if sd else None, name) for t in tasks for sd, name in states_of(t)
            if f"{t.name}|{name}" not in cache]
    print(f"{len(tasks)} tasks, {len(jobs)} new states to observe", flush=True)
    with ProcessPoolExecutor(max_workers=6) as ex:
        for o in ex.map(_obs_job, jobs):
            if "error" in o:
                print("  observe error", o["task"], o["state"], o["error"][:150])
                continue
            cache[f"{o['task']}|{o['state']}"] = o
    CACHE.write_text(json.dumps(cache))

    judge = CalibratedJudge(model=a.model, votes=a.votes, calibrator=Calibrator())   # uncalibrated: raw features
    todo = [k for k, o in cache.items() if "verdict" not in o]
    print(f"judging {len(todo)} states with {judge.model} x{a.votes} votes", flush=True)

    def run(k):
        o = cache[k]
        v = judge.verdict(o["prompt"], o["history"], o["raw"])
        return k, {"raw": v.raw, "progress": v.progress, "spread": v.spread, "evidence_ok": v.evidence_ok,
                   "rationale": v.rationale[:300]}

    with ThreadPoolExecutor(max_workers=3) as ex:
        for k, v in ex.map(run, todo):
            cache[k]["verdict"] = v
    CACHE.write_text(json.dumps(cache))

    rows = [o for o in cache.values() if "verdict" in o]
    for r in rows:
        r["feats"] = Calibrator.featurize(r["verdict"])
    tnum = lambda r: int(r["task"][1:3]) if r["task"][1:3].isdigit() else hash(r["task"])
    train = [r for r in rows if tnum(r) % 2 == 1]
    test = [r for r in rows if tnum(r) % 2 == 0]
    cal = Calibrator(meta={"model": judge.model, "votes": a.votes, "n_train": len(train), "n_test": len(test),
                           "label": "hidden-grader reward (soft)"})
    cal.fit([r["feats"] for r in train], [r["reward"] for r in train])
    out = [report("raw judge  / test", test, lambda r: r["verdict"]["raw"]),
           report("calibrated / test", test, lambda r: cal.predict(r["feats"])),
           report("raw judge  / train", train, lambda r: r["verdict"]["raw"]),
           report("calibrated / train", train, lambda r: cal.predict(r["feats"]))]
    # refit on everything for deployment
    final = Calibrator(meta={**cal.meta, "n_train": len(rows), "deployed_fit": "all states",
                             "test_report": out[:2]}).fit([r["feats"] for r in rows], [r["reward"] for r in rows])
    final.save()
    md = ["| set | n | AUROC (solved) | Brier (soft) | Brier (solved) | ECE (soft) | pairwise within task |",
          "|---|---|---|---|---|---|---|"]
    md += [f"| {o['set']} | {o['n']} | {o['auroc_solved']:.3f} | {o['brier_soft']:.3f} | {o['brier_solved']:.3f} | "
           f"{o['ece_soft']:.3f} | {o['pairwise_within_task']:.3f} |" for o in out]
    md.append(f"\nDeployed calibrator (fit on all {len(rows)} states): weights={json.dumps({k: round(v, 3) for k, v in final.w.items()})} "
              f"bias={final.b:.3f}. Judge {judge.model}, {a.votes} votes, judge cost ${judge.usd:.2f}.")
    (ROOT / "results" / "judge_calibration_report.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()
