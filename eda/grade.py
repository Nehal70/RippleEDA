"""
RippleEDA task grader  —  the ground-truth verifier behind the PRM.

    python -m eda.grade tasks/T01_sync_fifo_repair                 # grade the buggy start/ (should FAIL)
    python -m eda.grade tasks/T01_sync_fifo_repair --ref           # grade solution/ (should PASS)
    python -m eda.grade tasks/T01_sync_fifo_repair path/to/cand    # grade an agent candidate (same layout as start/)
    python -m eda.grade tasks/T01_... --only lint,synth            # run a subset (cheap PRM probes)

A candidate directory mirrors the task's start/ directory (the agent edits a copy of start/).
It may also contain recipe.json (yosys synth recipe, see eda.tools.synth) for PPA tasks.

Returns JSON: {"solved", "reward" in [0,1], "steps": [{check, ok, score, metrics, ...}], "seconds"}
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

import yaml

from eda import tools

STOP = object()


def _load(task_dir):
    task_dir = Path(task_dir).resolve()
    spec = yaml.safe_load((task_dir / "task.yaml").read_text())
    spec["_dir"] = task_dir
    return spec


def _stage(spec, cand, work):
    """Copy task assets + candidate files into an isolated work dir."""
    td = spec["_dir"]
    for sub in ("tb", "formal", "ref", "mutants", "data"):
        if (td / sub).exists():
            shutil.copytree(td / sub, work / ("_" + sub if sub in ("ref",) else sub), dirs_exist_ok=True)
    shutil.copytree(cand, work / "cand", dirs_exist_ok=True)
    recipe = None
    if (work / "cand/recipe.json").exists():
        recipe = json.loads((work / "cand/recipe.json").read_text())
    return recipe


def _rtl(spec, root):
    return [str(root / f) for f in spec["rtl"]]


def _run_dv(kind, chk, spec, work, rtl, sub):
    """Run a DV artefact (sim / cocotb / formal) against a given RTL list in its own subdir."""
    d = work / sub
    d.mkdir(parents=True, exist_ok=True)
    cand = work / "cand"
    resolve = lambda fs: [str((cand / f) if (cand / f).exists() else (work / f)) for f in fs]
    if kind == "sim":
        return tools.sim_iverilog(rtl + resolve(chk["tb"]), chk.get("top", "tb"), cwd=d,
                                  params=chk.get("params"), defines=chk.get("defines"))
    if kind == "gls":
        return tools.gls(rtl, spec["top"], resolve(chk["tb"]), chk.get("top", "tb"), cwd=d, params=chk.get("params"))
    if kind == "cocotb":
        tdir = cand / chk["dir"] if (cand / chk["dir"] / f"{chk['module']}.py").exists() else work / chk["dir"]
        return tools.sim_cocotb(rtl + resolve(chk.get("extra_hdl", [])), chk["toplevel"], chk["module"], tdir, cwd=d,
                                parameters=chk.get("parameters"))
    if kind == "formal":
        return tools.formal(rtl + resolve(chk["files"]), chk["top"], cwd=d, mode=chk.get("mode", "prove"),
                            depth=chk.get("depth", 20), engine=chk.get("engine"))
    raise ValueError(kind)


def run_check(chk, spec, work, recipe, state):
    t = chk["type"]
    rtl = _rtl(spec, work / "cand")
    lib = spec.get("lib", "sky130hd")
    if t == "lint":
        r = tools.lint(rtl, spec["top"], cwd=work, waive=chk.get("waive"))
        m = r["metrics"]
        r["score"] = 0.0 if m["errors"] else max(0.0, 1.0 - 0.15 * m["warnings"])
        if chk.get("gate", True) and m["errors"]:
            r["gate_failed"] = True
        return r
    if t in ("sim", "gls", "cocotb", "formal"):
        variants = chk.get("variants") or [{}]
        subs = []
        for i, v in enumerate(variants):
            c = {**chk, **v}
            subs.append(_run_dv(t, c, spec, work, rtl, f"{t}_{i}"))
        if t == "formal" and chk.get("mode") == "cover":
            score = sum(s["metrics"]["cover_reached"] / max(1, s["metrics"]["cover_reached"] + s["metrics"]["cover_unreached"]) for s in subs) / len(subs)
        elif t == "formal":
            score = sum(s["ok"] for s in subs) / len(subs)
        else:
            score = sum(s["metrics"]["passed"] / max(1, s["metrics"]["total"]) for s in subs) / len(subs)
        r = subs[0] if len(subs) == 1 else {"tool": t, "ok": all(s["ok"] for s in subs), "variants": subs,
                                            "metrics": {"variants_ok": sum(s["ok"] for s in subs), "variants": len(subs)}}
        r["score"] = round(score, 4) if not r["ok"] else 1.0
        return r
    if t == "synth":
        r = tools.synth(rtl, spec["top"], cwd=work, lib=lib, recipe=recipe)
        state["synth"] = r
        if not r["ok"]:
            r["score"] = 0.0
            return r
        m, viol, score = r["metrics"], [], 1.0
        if chk.get("no_latches", True) and (m["latches"] or m["latch_warnings"]):
            viol.append(f"latches={m['latches']}"); score -= 0.5
        if m["logic_loops"]:
            viol.append("logic loop"); score -= 0.5
        for key, metric in (("max_area", "area"), ("max_cells", "cells"), ("max_flops", "flops")):
            if key in chk and m[metric] > chk[key]:
                viol.append(f"{metric}={m[metric]} > {chk[key]}")
                score = min(score, chk[key] / m[metric])
        r["violations"], r["ok"], r["score"] = viol, not viol, round(max(0.0, score), 4)
        return r
    if t == "sta":
        s = state.get("synth")
        if not s or not s.get("metrics"):
            return {"tool": "opensta", "ok": False, "score": 0.0, "metrics": {}, "log": "no netlist"}
        r = tools.sta(s["netlist"], spec["top"], cwd=work, lib=lib, period_ns=chk["period_ns"],
                      clock_port=chk.get("clock", "clk"))
        state["sta"] = r
        w = r["metrics"]["wns"]
        r["score"] = 0.0 if w is None else (1.0 if w >= 0 else round(max(0.0, 1 + w / chk["period_ns"]), 4))
        return r
    if t == "equiv":
        gold = [str(work / "_ref" / Path(f).name) for f in chk["gold"]]
        if chk.get("engine", "smt") == "smt":      # word-level miter via sby+bitwuzla (default)
            r = tools.equiv_smt(gold, rtl, spec["top"], cwd=work, depth=max(1, chk.get("seq_depth", 0)),
                               split=chk.get("split"))
        else:                                      # bit-level yosys sat
            r = tools.equiv(gold, rtl, spec["top"], cwd=work, seq_depth=chk.get("seq_depth", 0))
        r["score"] = 1.0 if r["ok"] else 0.0
        return r
    if t == "mutants":
        # DV task: the candidate's testbench / properties must PASS on the golden RTL and FAIL on every mutant.
        kind = chk["kind"]
        gold_rtl = [str(work / "_ref" / Path(f).name) for f in spec["rtl"]]
        g = _run_dv(kind, chk, spec, work, gold_rtl, "mut_gold")
        killed, details = 0, []
        mdirs = sorted(p for p in (work / "mutants").iterdir() if p.is_dir())
        for md in mdirs:
            mrtl = [str(md / Path(f).name) if (md / Path(f).name).exists() else str(work / "_ref" / Path(f).name)
                    for f in spec["rtl"]]
            mr = _run_dv(kind, chk, spec, work, mrtl, f"mut_{md.name}")
            k = not mr["ok"]
            killed += k
            details.append({"mutant": md.name, "killed": k, "desc": (md / "DESC").read_text().strip() if (md / "DESC").exists() else ""})
        score = (killed / len(mdirs)) if g["ok"] else 0.0
        return {"tool": f"mutation[{kind}]", "ok": g["ok"] and killed == len(mdirs), "score": round(score, 4),
                "metrics": {"golden_pass": g["ok"], "killed": killed, "mutants": len(mdirs)},
                "golden_log": g["log"][-1500:], "mutants": details, "seconds": None}
    if t == "openroad_mock":
        if "synth" not in state or "sta" not in state:
            return {"tool": "openroad_mock", "ok": False, "score": 0.0, "metrics": {}}
        r = tools.openroad_mock(state["synth"], state["sta"], chk.get("util", 0.55))
        r["score"] = 1.0 if r["ok"] else 0.5
        return r
    raise ValueError(f"unknown check {t}")


def grade(task_dir, cand=None, only=None, keep=False):
    spec = _load(task_dir)
    cand = Path(cand) if cand else spec["_dir"] / "start"
    work = Path(tempfile.mkdtemp(prefix=f"grade_{spec['id']}_"))
    t0 = time.time()
    recipe = _stage(spec, cand, work)
    steps, state, gated = [], {}, False
    for chk in spec["checks"]:
        if only and chk["type"] not in only:
            continue
        w = chk.get("weight", 1.0)
        if gated:
            steps.append({"check": chk["type"], "ok": False, "score": 0.0, "weight": w, "skipped": "gate"})
            continue
        try:
            r = run_check(chk, spec, work, recipe, state)
        except Exception as e:  # a broken candidate must never crash the search
            r = {"ok": False, "score": 0.0, "metrics": {}, "log": f"grader exception: {e!r}"}
        r.update(check=chk["type"], weight=w, required=chk.get("required", chk["type"] != "openroad_mock"))
        steps.append(r)
        gated = gated or r.get("gate_failed", False)
    tw = sum(s["weight"] for s in steps) or 1
    out = {"task": spec["id"], "candidate": str(cand), "solved": all(s["ok"] for s in steps if s.get("required", True)),
           "reward": round(sum(s["score"] * s["weight"] for s in steps) / tw, 4),
           "steps": steps, "seconds": round(time.time() - t0, 2), "workdir": str(work)}
    if not keep:
        shutil.rmtree(work, ignore_errors=True)
        out.pop("workdir")
    return out


def summarize(res):
    lines = [f"[{res['task']}] solved={res['solved']} reward={res['reward']} ({res['seconds']}s)"]
    for s in res["steps"]:
        m = {k: v for k, v in (s.get("metrics") or {}).items() if k not in ("cells_by_type", "by_code")}
        lines.append(f"  {'PASS' if s['ok'] else 'FAIL'} {s['check']:<14} score={s['score']:<6} {json.dumps(m)[:160]}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task")
    ap.add_argument("candidate", nargs="?")
    ap.add_argument("--ref", action="store_true", help="grade the task solution/ dir")
    ap.add_argument("--only", help="comma list of check types")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--keep", action="store_true", help="keep work dir (logs, netlist, vcd)")
    a = ap.parse_args()
    spec = _load(a.task)
    cand = spec["_dir"] / "solution" if a.ref else a.candidate
    res = grade(a.task, cand, a.only.split(",") if a.only else None, a.keep)
    print(json.dumps(res, indent=1, default=str) if a.json else summarize(res))
    sys.exit(0)


if __name__ == "__main__":
    main()
