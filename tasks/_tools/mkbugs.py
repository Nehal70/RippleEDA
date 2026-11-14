"""
Build a multi-bug task's start/ and partial/ dirs from its solution, and prove every bug is caught by the
public smoke test on its own (independence) -> the public PRM gets one step of reward per fix.

    python tasks/_tools/mkbugs.py tasks/T22_xxx bugs.json
bugs.json: {"rtl": "rtl/x.sv", "smoke_tb": "tb/tb_smoke.sv", "smoke_top": "tb_smoke", "extra_tb": [],
            "bugs": {"name": [["old", "new"], ...], ...}, "order": ["name", ...]}
"""
import json
import os
import re
import subprocess
import sys

T, cfg = sys.argv[1], json.load(open(sys.argv[2]))
sol = open(f"{T}/solution/{cfg['rtl']}").read()


def apply(src, names):
    for n in names:
        for a, b in cfg["bugs"][n]:
            assert a in src, f"{n}: pattern not found: {a[:60]!r}"
            src = src.replace(a, b, 1)
    return src


def smoke(src, tag):
    d = f"/tmp/claude-501/mkbugs/{os.path.basename(T)}/{tag}"
    os.makedirs(d, exist_ok=True)
    open(f"{d}/dut.sv", "w").write(src)
    tbs = " ".join(f"{T}/start/{t}" for t in [cfg["smoke_tb"]] + cfg.get("extra_tb", []))
    r = subprocess.run(f"cd {d} && iverilog -g2012 -o s -s {cfg['smoke_top']} dut.sv {tbs} && vvp -n s",
                       shell=True, capture_output=True, text=True, timeout=300)
    m = re.findall(r"RESULT (\d+) (\d+)", r.stdout)
    first = [l for l in r.stdout.splitlines() if l.startswith("ERROR")][:1]
    return (f"{m[-1][0]}/{m[-1][1]}" if m else "COMPILE/RUN FAIL " + (r.stderr or r.stdout)[-200:]), first


ok = True
print(f"{'reference':<28}", *smoke(sol, "ref"))
for n in cfg["bugs"]:
    res, first = smoke(apply(sol, [n]), n)
    caught = not res.startswith("COMPILE") and res.split("/")[0] != res.split("/")[1]
    ok &= caught
    print(f"{n:<28} {res:<12} {'caught' if caught else '<-- NOT CAUGHT'} {first}")
names = list(cfg["bugs"])
start = apply(sol, names)
os.makedirs(os.path.dirname(f"{T}/start/{cfg['rtl']}"), exist_ok=True)
open(f"{T}/start/{cfg['rtl']}", "w").write(start)
print(f"{'ALL (start)':<28}", *smoke(start, "all"))
order = cfg.get("order", names)
for k in sorted({len(order) // 2, len(order) - 1}):
    if k <= 0:
        continue
    src = apply(sol, order[k:])
    p = f"{T}/partial/fixed_{k}_of_{len(order)}/{cfg['rtl']}"
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w").write(src)
    print(f"partial fixed {k}/{len(order):<14}", *smoke(src, f"p{k}"))
sys.exit(0 if ok else 1)
