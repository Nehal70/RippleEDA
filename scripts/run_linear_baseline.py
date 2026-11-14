"""Linear (plain ReAct) Ripple baseline over the generated tasks, graded by the hidden grader.

One `agent.agents.run_linear` trajectory per task (max_calls=30, temperature 0.6, fixed seed), tasks run in
parallel. Per-task results are written as soon as each finishes so partial progress survives; summary.txt is
rewritten after every task. Re-running skips tasks that already have a result file (use --force to redo).

    source ~/rippleeda/env.sh            # (or env_mcts.sh) tools + OPENROUTER_API_KEY from .env
    python3 scripts/run_linear_baseline.py                       # all tasks/G_* -> results/linear_baseline_v2/
    python3 scripts/run_linear_baseline.py --tasks G_fifo_01 G_alu_02 --workers 2
    python3 scripts/run_linear_baseline.py --summarize           # no API calls, rebuild summary.txt
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.agents import run_linear          # noqa: E402
from agent.llm import MODEL, RippleClient, Usage   # noqa: E402

TASK_GLOB = "G_*"


def _final_files(ws, clip=20000):
    out = {}
    for p in sorted(ws.root.rglob("*")):
        rel = p.relative_to(ws.root)
        if p.is_file() and not any(x.startswith(("_run", "_witness", ".")) for x in rel.parts):
            txt = p.read_text(errors="ignore")
            out[str(rel)] = txt if len(txt) <= clip else txt[:clip] + "\n...[truncated]"
    return out


def one_task(task_dir: Path, args):
    usage = Usage()
    client = RippleClient(model=args.model, usage=usage)
    t0 = time.time()
    res, ws, messages = run_linear(task_dir, client, max_calls=args.max_calls, temperature=args.temperature,
                                   seed=args.seed)
    rec = {"task": task_dir.name, "model": args.model, "seed": args.seed, "max_calls": args.max_calls,
           "temperature": args.temperature, "prompt": ws.spec["prompt"].strip(),
           "calls": res["calls"], "submitted": res["submitted"], "seconds": res["seconds"],
           "wall_seconds": round(time.time() - t0, 1), "final": res["final"], "usage": usage.as_dict(),
           "tools_used": [a["tool"] for t in res["trace"] for a in t["actions"]],
           "trace": res["trace"], "messages": messages, "final_files": _final_files(ws)}
    ws.cleanup()
    return rec


def summarize(out_dir: Path, n_total: int | None = None):
    recs = []
    for f in sorted(out_dir.glob("G_*.json")):
        try:
            recs.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    if not recs:
        return "no results yet\n"
    n = len(recs)
    solved = sum(1 for r in recs if r["final"]["solved"])
    reward = sum(r["final"]["reward"] for r in recs) / n
    calls = sum(r["calls"] for r in recs) / n
    secs = sum(r["seconds"] for r in recs) / n
    usd = sum(r["usage"]["usd"] for r in recs)
    submitted = sum(1 for r in recs if r["submitted"])
    wrote_tb = sum(1 for r in recs if any(k.endswith((".sv", ".v")) and not k.startswith("rtl/") for k in r["final_files"]))
    ran_sim = sum(1 for r in recs if "run_sim" in r["tools_used"])
    lines = [f"linear baseline v2  model={recs[0]['model']}  tasks={n}" + (f"/{n_total}" if n_total else ""),
             f"solved {solved}/{n} ({solved / n:.0%})  mean_reward={reward:.3f}  mean_calls={calls:.1f}  "
             f"mean_seconds={secs:.0f}  total_cost=${usd:.4f}",
             f"submitted {submitted}/{n}  wrote_own_tb {wrote_tb}/{n}  ran_sim {ran_sim}/{n}", ""]
    by_fam = {}
    for r in recs:
        fam = r["task"].split("_")[1]
        by_fam.setdefault(fam, [0, 0])
        by_fam[fam][0] += r["final"]["solved"]
        by_fam[fam][1] += 1
    lines.append("per family: " + "  ".join(f"{k}={v[0]}/{v[1]}" for k, v in sorted(by_fam.items())))
    lines.append("")
    lines.append(f"{'task':<12} {'solved':<7} {'reward':>6} {'calls':>5} {'secs':>6} {'usd':>7}  {'failing checks':<28} tools")
    for r in recs:
        fails = ",".join(s["check"] for s in r["final"]["steps"] if not s["ok"]) or "-"
        tools = ",".join(f"{t}x{c}" for t, c in sorted(
            {t: r["tools_used"].count(t) for t in set(r["tools_used"])}.items(), key=lambda x: -x[1]))
        lines.append(f"{r['task']:<12} {str(r['final']['solved']):<7} {r['final']['reward']:>6.3f} {r['calls']:>5} "
                     f"{r['seconds']:>6.0f} {r['usage']['usd']:>7.4f}  {fails:<28} {tools}")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(ROOT / "results" / "linear_baseline_v2"))
    ap.add_argument("--tasks", nargs="*", help="explicit task ids (default: every tasks/G_*)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--max-calls", type=int, default=30)
    ap.add_argument("--temperature", type=float, default=0.6)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--deadline-min", type=float, default=0, help="stop starting new tasks after this many minutes")
    ap.add_argument("--claim-stale-min", type=float, default=45, help="ignore another runner's claim older than this")
    ap.add_argument("--force", action="store_true", help="redo tasks that already have a result file")
    ap.add_argument("--summarize", action="store_true", help="no API calls; rebuild summary.txt from results")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    task_dirs = sorted(p for p in (ROOT / "tasks").glob(TASK_GLOB) if (p / "task.yaml").exists())
    if args.tasks:
        task_dirs = [p for p in task_dirs if p.name in set(args.tasks)]
    if args.limit:
        task_dirs = task_dirs[: args.limit]
    n_total = len(task_dirs)
    if args.summarize:
        s = summarize(out_dir, n_total)
        (out_dir / "summary.txt").write_text(s, encoding="utf-8")
        print(s)
        return

    todo = [p for p in task_dirs if args.force or not (out_dir / f"{p.name}.json").exists()]
    print(f"model={args.model} tasks={n_total} todo={len(todo)} workers={args.workers} max_calls={args.max_calls} "
          f"seed={args.seed} out={out_dir}", flush=True)
    lock = threading.Lock()
    t0 = time.time()
    stop = threading.Event()

    def claim(p):
        """Atomic per-task claim so several runner processes can share one results dir without double-spending.
        A claim older than --claim-stale-min with no result is treated as abandoned."""
        c = out_dir / f"{p.name}.claim"
        if c.exists() and (time.time() - c.stat().st_mtime) > args.claim_stale_min * 60:
            c.unlink(missing_ok=True)
        try:
            fd = os.open(c, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        os.write(fd, f"{os.getpid()} {time.strftime('%Y-%m-%d %H:%M:%S')}\n".encode()); os.close(fd)
        return True

    def work(p):
        if stop.is_set() or (out_dir / f"{p.name}.json").exists() and not args.force:
            return p.name, None
        if not claim(p):
            return p.name, "claimed"
        try:
            return p.name, one_task(p, args)
        finally:
            (out_dir / f"{p.name}.claim").unlink(missing_ok=True)

    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(work, p): p.name for p in todo}
        for fut in as_completed(futs):
            name = futs[fut]
            try:
                _, rec = fut.result()
            except Exception as e:
                print(f"  {name}: ERROR {type(e).__name__}: {str(e)[:300]}", flush=True)
                with lock:
                    (out_dir / f"{name}.error.txt").write_text(f"{type(e).__name__}: {e}\n", encoding="utf-8")
                continue
            if rec is None or rec == "claimed":
                print(f"  {name}: skipped ({'claimed by another runner' if rec else 'deadline / already done'})", flush=True)
                continue
            with lock:
                (out_dir / f"{name}.json").write_text(json.dumps(rec, indent=1, default=str), encoding="utf-8")
                (out_dir / f"{name}.error.txt").unlink(missing_ok=True)
                done += 1
                s = summarize(out_dir, n_total)
                (out_dir / "summary.txt").write_text(s, encoding="utf-8")
            f = rec["final"]
            print(f"[{done:2}/{len(todo)}] {name:12} solved={str(f['solved']):<5} reward={f['reward']:.3f} "
                  f"calls={rec['calls']:2} {rec['seconds']:5.0f}s ${rec['usage']['usd']:.4f}  "
                  f"elapsed {(time.time() - t0) / 60:.0f}min", flush=True)
            if args.deadline_min and (time.time() - t0) / 60 > args.deadline_min and not stop.is_set():
                stop.set()
                print(f"  deadline reached: no new tasks will start", flush=True)
    print("\n" + summarize(out_dir, n_total))


if __name__ == "__main__":
    main()
