"""Compose multi-bug repair tasks from verified bug catalogs.

Each catalog lives in tasks/_gen/<design>/:
  catalog.json  {"top", "rtl", "prompt", "hidden_tb": [...], "hidden_top", "hidden_variants": [...],
                 "smoke_tb", "smoke_top", "sta": {"period_ns", "clock"} | null, "lint": true,
                 "bugs": {name: [[old, new], ...]}}
  solution/<rtl>, tb/<hidden tbs>, start_tb/<smoke tb>
Every bug in a catalog is first verified to be caught ON ITS OWN by the public smoke test (`--verify`), so each
fix a search makes moves the public reward. A task = the reference with k catalog bugs applied.

    python tasks/_gen/generate.py --verify spi            # prove every catalog bug is caught alone
    python tasks/_gen/generate.py --make spi --n 5 --k 5   # write tasks/G_spi_01..05
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import random
import re
import shutil
import subprocess
from pathlib import Path

GEN = Path(__file__).resolve().parent
TASKS = GEN.parent


def load(design):
    d = GEN / design
    cat = json.loads((d / "catalog.json").read_text())
    cat["_dir"] = d
    cat["_sol"] = (d / "solution" / cat["rtl"]).read_text()
    return cat


def apply(cat, src, names):
    order = list(cat["bugs"])
    for n in sorted(names, key=order.index):      # always catalog order: later bugs may edit earlier bugs' text
        for a, b in cat["bugs"][n]:
            if a not in src:
                raise KeyError(f"{n}: pattern not found (conflicts with another bug?): {a[:70]!r}")
            src = src.replace(a, b, 1)
    return src


def smoke(cat, src, tag):
    d = Path(f"/tmp/claude-501/gen/{cat['_dir'].name}/{tag}")
    d.mkdir(parents=True, exist_ok=True)
    (d / "dut.sv").write_text(src)
    tb = cat["_dir"] / "start_tb" / cat["smoke_tb"]
    r = subprocess.run(f"cd {d} && iverilog -g2012 -o s -s {cat['smoke_top']} dut.sv {tb} && vvp -n s",
                       shell=True, capture_output=True, text=True, timeout=600)
    m = re.findall(r"RESULT (\d+) (\d+)", r.stdout)
    if not m:
        return None, (r.stderr or r.stdout)[-300:]
    return (int(m[-1][0]), int(m[-1][1])), [l for l in r.stdout.splitlines() if l.startswith("ERROR")][:1]


def verify(design):
    cat = load(design)
    ref, info = smoke(cat, cat["_sol"], "ref")
    print(f"{design}: reference {ref} {info if ref is None else ''}")
    assert ref and ref[0] == ref[1], "reference must pass its own smoke test"
    bad = []
    for n in cat["bugs"]:
        res, first = smoke(cat, apply(cat, cat["_sol"], [n]), n)
        caught = res is None or res[0] < res[1]
        print(f"  {n:<32} {res if res else 'compile/run fail (counts as caught)'} {'caught' if caught else '<-- NOT CAUGHT'} {first}")
        if not caught:
            bad.append(n)
    # pairwise composability: every pair of bugs must apply together
    clash = []
    for a, b in itertools.combinations(cat["bugs"], 2):
        try:
            apply(cat, cat["_sol"], [a, b])
        except KeyError:
            clash.append((a, b))
    print(f"  pairs that cannot be combined: {clash or 'none'}")
    return not bad, clash


def make(design, n, k, seed=0, prefix=None):
    cat = load(design)
    _, clash = verify(design)
    clash = {frozenset(c) for c in clash}
    rng = random.Random(f"{design}-{seed}")
    names = list(cat["bugs"])
    made, tries = [], 0
    seen = set()
    while len(made) < n and tries < 5000:
        tries += 1
        combo = tuple(sorted(rng.sample(names, k)))
        if combo in seen or any(frozenset(p) in clash for p in itertools.combinations(combo, 2)):
            continue
        seen.add(combo)
        idx = len(made) + 1
        tid = f"G_{prefix or design}_{idx:02d}"
        td = TASKS / tid
        if td.exists():
            shutil.rmtree(td)
        (td / "solution" / Path(cat["rtl"]).parent).mkdir(parents=True)
        (td / "start" / Path(cat["rtl"]).parent).mkdir(parents=True)
        (td / "start" / "tb").mkdir(parents=True)
        (td / "solution" / cat["rtl"]).write_text(cat["_sol"])
        start = apply(cat, cat["_sol"], combo)
        (td / "start" / cat["rtl"]).write_text(start)
        shutil.copy(cat["_dir"] / "start_tb" / cat["smoke_tb"], td / "start" / "tb" / cat["smoke_tb"])
        shutil.copytree(cat["_dir"] / "tb", td / "tb")
        # partial fixes (public-reward staircase): fix half, and all but one
        order = list(combo)
        rng.shuffle(order)
        for kk in sorted({len(order) // 2, len(order) - 1}):
            if kk > 0:
                p = td / "partial" / f"fixed_{kk}_of_{len(order)}" / cat["rtl"]
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(apply(cat, cat["_sol"], order[kk:]))
        checks = []
        if cat.get("lint", True):
            checks.append("  - {type: lint, weight: 1}")
        variants = json.dumps(cat.get("hidden_variants") or [{}])
        checks.append(f"  - {{type: sim, tb: {json.dumps(['tb/' + t for t in cat['hidden_tb']])}, top: {cat['hidden_top']}, "
                      f"weight: 4, variants: {variants}}}")
        checks.append("  - {type: synth, no_latches: true, weight: 1}")
        if cat.get("sta"):
            checks.append(f"  - {{type: sta, period_ns: {cat['sta']['period_ns']}, clock: {cat['sta'].get('clock', 'clk')}, weight: 1}}")
        prompt = "\n".join("  " + l for l in cat["prompt"].strip().splitlines())
        (td / "task.yaml").write_text(
            f"id: {tid}\nname: {design}_multibug_{idx:02d}\ncategory: rtl-repair\ndifficulty: hard\n"
            f"top: {cat['top']}\nrtl: [{cat['rtl']}]\nlib: sky130hd\n"
            f"prompt: |\n{prompt}\n"
            f"public_checks:   # run by the honest PRM (files from the original start/, agent edits ignored)\n"
            f"  - {{type: sim, tb: [tb/{cat['smoke_tb']}], top: {cat['smoke_top']}}}\n"
            f"checks:\n" + "\n".join(checks) + "\n"
            f"generated_from: {design}\nbugs: {json.dumps(list(combo))}\n"
            f"why_mcts: |\n  {k} independent bugs drawn from a verified catalog; each is caught on its own by the public\n"
            f"  smoke test, so every verified fix raises the public reward. A single trajectory must find and fix\n"
            f"  all {k} without regressing; search keeps each fix as a node.\n")
        made.append((tid, combo))
        print(f"  wrote {tid}: {combo}")
    return made


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify")
    ap.add_argument("--make")
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    if a.verify:
        ok, _ = verify(a.verify)
        raise SystemExit(0 if ok else 1)
    if a.make:
        make(a.make, a.n, a.k, a.seed)
