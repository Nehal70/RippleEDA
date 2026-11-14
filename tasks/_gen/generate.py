"""Compose multi-bug repair tasks from verified bug catalogs.

Each catalog lives in tasks/_gen/<design>/:
  catalog.json  {"top", "rtl", "prompt", "hidden_tb": [...], "hidden_top", "hidden_variants": [...],
                 "smoke_tb", "smoke_top", "sta": {"period_ns", "clock"} | null, "lint": true,
                 "bugs": {name: [[old, new], ...]}}
  solution/<rtl>, tb/<hidden tbs>, start_tb/<smoke tb>

v2 task format (the agent gets NO spec in the prompt and NO shipped testbench):
  * the behavioural spec lives in a `// SPEC:` header comment at the top of solution/<rtl>; the generator carries
    that header verbatim into start/, solution/ and partial/ (bugs are applied to the reference text, which
    includes the header), and `--verify` checks that no bug's old-pattern collides with the header text.
  * task.yaml prompt is a one-liner ("rtl/x.sv isn't passing. Fix it. Keep the port list." + the timing target
    when the grader has an STA check).  No public_checks, no start/tb/: the agent must write its own tests.
  * the smoke testbench in start_tb/ is generator-internal only: `--verify` still proves every catalog bug is
    caught on its own (so the catalog stays honest), but it is never shipped.
  * optional synth budget: `max_cells = ceil(1.1 * reference cells)` measured with the grader's own synth flow.

    python tasks/_gen/generate.py --verify spi            # prove every catalog bug is caught alone
    python tasks/_gen/generate.py --make spi --n 5 --k 5   # write tasks/G_spi_01..05
    python tasks/_gen/generate.py --all                    # regenerate all 50 tasks (recorded n/k per design)
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

GEN = Path(__file__).resolve().parent
TASKS = GEN.parent
sys.path.insert(0, str(TASKS.parent))      # for eda.tools (reference cell count)

HEADER_MARK = "// SPEC:"
# (n tasks, k bugs each) used for the shipped 50-task set; seed 0. Same RNG stream as v1 -> same bug combos.
SHIPPED = {"afifo": (5, 4), "alu": (8, 6), "div": (6, 5), "fifo": (7, 5), "irq": (6, 5), "mac": (6, 4),
           "spi": (6, 5), "uart": (6, 4)}


def load(design):
    d = GEN / design
    cat = json.loads((d / "catalog.json").read_text())
    cat["_dir"] = d
    cat["_sol"] = (d / "solution" / cat["rtl"]).read_text()
    return cat


def header(src):
    """Leading `//` comment block of a source file (the SPEC header)."""
    out = []
    for line in src.splitlines(keepends=True):
        if line.startswith("//"):
            out.append(line)
        else:
            break
    return "".join(out)


def apply(cat, src, names):
    order = list(cat["bugs"])
    for n in sorted(names, key=order.index):      # always catalog order: later bugs may edit earlier bugs' text
        for a, b in cat["bugs"][n]:
            if a not in src:
                raise KeyError(f"{n}: pattern not found (conflicts with another bug?): {a[:70]!r}")
            src = src.replace(a, b, 1)
    return src


def smoke(cat, src, tag):
    d = Path(tempfile.gettempdir()) / "rippleeda_gen" / cat["_dir"].name / tag
    d.mkdir(parents=True, exist_ok=True)
    (d / "dut.sv").write_text(src)
    tb = cat["_dir"] / "start_tb" / cat["smoke_tb"]
    r = subprocess.run(f"cd {d} && iverilog -g2012 -o s -s {cat['smoke_top']} dut.sv {tb} && vvp -n s",
                       shell=True, capture_output=True, text=True, timeout=600)
    m = re.findall(r"RESULT (\d+) (\d+)", r.stdout)
    if not m:
        return None, (r.stderr or r.stdout)[-300:]
    return (int(m[-1][0]), int(m[-1][1])), [l for l in r.stdout.splitlines() if l.startswith("ERROR")][:1]


def check_header(cat):
    """The SPEC header must exist and no bug's old-pattern may match inside it (bugs edit code, not the spec)."""
    hdr = header(cat["_sol"])
    assert hdr.startswith("// ---") and HEADER_MARK in hdr, f"{cat['_dir'].name}: solution RTL has no SPEC header"
    bad = [(n, a[:50]) for n, pairs in cat["bugs"].items() for a, _ in pairs if a in hdr]
    assert not bad, f"{cat['_dir'].name}: bug patterns collide with the SPEC header: {bad}"
    return hdr


def verify(design):
    cat = load(design)
    hdr = check_header(cat)
    print(f"{design}: SPEC header ok ({len(hdr.splitlines())} lines, no bug-pattern collisions)")
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


def ref_cells(cat):
    """Reference cell count with the grader's own synth flow (eda.tools.synth, sky130hd). None if unavailable."""
    try:
        from eda import tools
        d = Path(tempfile.mkdtemp(prefix="gen_cells_"))
        r = tools.synth([str(cat["_dir"] / "solution" / cat["rtl"])], cat["top"], cwd=d)
        shutil.rmtree(d, ignore_errors=True)
        return r["metrics"]["cells"] if r["ok"] else None
    except Exception as e:
        print(f"  (reference synth failed: {e!r}; no max_cells)")
        return None


def prompt_for(cat):
    p = f"{cat['rtl']} isn't passing. Fix it. Keep the port list."
    if cat.get("sta"):
        clk = cat["sta"].get("clock", "clk")
        p += f" It must close timing at {cat['sta']['period_ns']:g} ns" + (f" on {clk}." if clk != "clk" else ".")
    return p


def make(design, n, k, seed=0, prefix=None, max_cells=True):
    cat = load(design)
    _, clash = verify(design)
    clash = {frozenset(c) for c in clash}
    hdr = header(cat["_sol"])
    cells = ref_cells(cat) if max_cells else None
    budget = math.ceil(1.1 * cells) if cells else None
    print(f"  reference cells={cells} -> max_cells={budget}")
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
        (td / "solution" / cat["rtl"]).write_text(cat["_sol"])
        start = apply(cat, cat["_sol"], combo)
        assert start.startswith(hdr), "SPEC header must survive bug injection"
        (td / "start" / cat["rtl"]).write_text(start)
        shutil.copytree(cat["_dir"] / "tb", td / "tb")          # hidden grader testbenches only
        # partial fixes (reward staircase for analysis): fix half, and all but one
        order = list(combo)
        rng.shuffle(order)
        for kk in sorted({len(order) // 2, len(order) - 1}):
            if kk > 0:
                p = td / "partial" / f"fixed_{kk}_of_{len(order)}" / cat["rtl"]
                p.parent.mkdir(parents=True, exist_ok=True)
                src = apply(cat, cat["_sol"], order[kk:])
                assert src.startswith(hdr)
                p.write_text(src)
        checks = []
        if cat.get("lint", True):
            checks.append("  - {type: lint, weight: 1}")
        variants = json.dumps(cat.get("hidden_variants") or [{}])
        checks.append(f"  - {{type: sim, tb: {json.dumps(['tb/' + t for t in cat['hidden_tb']])}, top: {cat['hidden_top']}, "
                      f"weight: 4, variants: {variants}}}")
        synth = "  - {type: synth, no_latches: true" + (f", max_cells: {budget}" if budget else "") + ", weight: 1}"
        checks.append(synth)
        if cat.get("sta"):
            checks.append(f"  - {{type: sta, period_ns: {cat['sta']['period_ns']}, clock: {cat['sta'].get('clock', 'clk')}, weight: 1}}")
        (td / "task.yaml").write_text(
            f"id: {tid}\nname: {design}_multibug_{idx:02d}\ncategory: rtl-repair\ndifficulty: hard\n"
            f"top: {cat['top']}\nrtl: [{cat['rtl']}]\nlib: sky130hd\n"
            f"prompt: |\n  {prompt_for(cat)}\n"
            f"checks:\n" + "\n".join(checks) + "\n"
            f"generated_from: {design}\nbugs: {json.dumps(list(combo))}\n"
            + (f"reference_cells: {cells}\n" if cells else "")
            + f"why_mcts: |\n  {k} independent bugs drawn from a verified catalog. The prompt carries no spec and no testbench:\n"
            f"  the behavioural contract is only in the SPEC header of the RTL, so the agent must read it, write its\n"
            f"  own tests, and find and fix all {k} bugs without regressing; search keeps each verified fix as a node.\n")
        made.append((tid, combo))
        print(f"  wrote {tid}: {combo}")
    return made


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify")
    ap.add_argument("--make")
    ap.add_argument("--all", action="store_true", help="regenerate every shipped design with its recorded n/k")
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--no-max-cells", action="store_true", help="skip the reference synth / max_cells budget")
    a = ap.parse_args()
    if a.verify:
        ok, _ = verify(a.verify)
        raise SystemExit(0 if ok else 1)
    if a.make:
        make(a.make, a.n, a.k, a.seed, max_cells=not a.no_max_cells)
    if a.all:
        for design, (n, k) in SHIPPED.items():
            make(design, n, k, a.seed, max_cells=not a.no_max_cells)
