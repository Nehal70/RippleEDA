"""
RippleEDA tool wrappers.

Every function runs one real EDA tool in a work directory and returns a plain
dict (JSON-serialisable) with:  ok (bool), metrics (dict), log (tail of tool
output), seconds (wall time).  These are the "observations" your PRM scores and
the "actions" your MCTS / Ripple-65B tool calls map onto.

Requires `source ~/rippleeda/env.sh` (puts yosys/verilator/iverilog/sby/sta on PATH).
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

HOME = Path(os.environ.get("RIPPLEEDA_HOME", Path.home() / "rippleeda"))
LIBS = {
    "sky130hd": Path(os.environ.get("SKY130_LIB", HOME / "pdk/sky130hd/sky130_fd_sc_hd__tt_025C_1v80.lib")),
    "nangate45": Path(os.environ.get("NANGATE_LIB", HOME / "pdk/nangate45/nangate45_typ.lib")),
}
# cells we never want ABC / dfflibmap to pick in sky130hd
SKY130_DONT_USE = ["*lpflow*", "*probe*", "*clkdly*", "*dlygate*", "*dlymetal*", "*buf_16*"]

DEFAULT_LINT_WAIVE = ["DECLFILENAME", "UNUSEDSIGNAL", "UNUSEDPARAM", "PINCONNECTEMPTY", "EOFNEWLINE"]


def _run(cmd, cwd, timeout=60, input_text=None):
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, input=input_text)
        out, rc = p.stdout + p.stderr, p.returncode
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or b"").decode(errors="ignore") if isinstance(e.stdout, bytes) else (e.stdout or "")) + "\nTIMEOUT"
        rc = -9
    return rc, out, round(time.time() - t0, 3)


def _tail(s, n=40):
    return "\n".join(s.strip().splitlines()[-n:])


def _lib(lib):
    return str(LIBS.get(lib, lib))


# --------------------------------------------------------------------------- lint
def lint(files, top, cwd=".", waive=None, extra_args=None, timeout=20):
    """Verilator --lint-only -Wall. Returns error/warning counts by code."""
    waive = DEFAULT_LINT_WAIVE + list(waive or [])
    cmd = ["verilator", "--lint-only", "-Wall", "-Wno-fatal", "--top-module", top]
    cmd += [f"-Wno-{w}" for w in waive] + list(extra_args or []) + list(files)
    rc, out, sec = _run(cmd, cwd, timeout)
    warnings, errors = {}, []
    for line in out.splitlines():
        m = re.match(r"%Warning-(\w+):\s*(.*)", line)
        if m:
            warnings.setdefault(m.group(1), []).append(m.group(2).strip())
            continue
        m = re.match(r"%Error(?:-(\w+))?:\s*(.*)", line)
        if m and "Exiting due to" not in line:
            errors.append(((m.group(1) or "SYNTAX") + ": " + m.group(2)).strip())
    n_warn = sum(len(v) for v in warnings.values())
    return {
        "tool": "verilator_lint", "ok": rc == 0 and not errors and n_warn == 0,
        "metrics": {"errors": len(errors), "warnings": n_warn,
                    "by_code": {k: len(v) for k, v in warnings.items()},
                    "latch": len(warnings.get("LATCH", [])) + len(warnings.get("COMBDLY", [])),
                    "width": sum(len(v) for k, v in warnings.items() if k.startswith("WIDTH")),
                    "comb_loop": len(warnings.get("UNOPTFLAT", [])) + len(warnings.get("ALWCOMBORDER", []))},
        "messages": errors + [f"{k}: {m}" for k, v in warnings.items() for m in v],
        "log": _tail(out), "seconds": sec,
    }


# --------------------------------------------------------------------------- synth
def _norm_recipe(r):
    """Agents write recipe.json loosely: accept lists or strings for every field."""
    if not isinstance(r, dict):
        return {}
    out = {}
    for k in ("synth_args", "abc_args", "abc_script"):
        v = r.get(k)
        if v is not None:
            out[k] = (";".join if k == "abc_script" else " ".join)(map(str, v)) if isinstance(v, (list, tuple)) else str(v)
    for k in ("pre", "post"):
        v = r.get(k)
        if v is not None:
            out[k] = [str(x) for x in v] if isinstance(v, (list, tuple)) else [x.strip() for x in str(v).split(";") if x.strip()]
    return out


def synth(files, top, cwd=".", lib="sky130hd", recipe=None, netlist="netlist.v", timeout=120):
    """
    Yosys synthesis to a liberty library.  `recipe` lets the search explore:
      synth_args : extra args to `synth`            e.g. "-booth", "-noabc9", "-flatten"
      pre        : yosys cmds before techmap        e.g. ["opt -full", "share -aggressive", "fsm_recode -encoding one-hot"]
      abc_args   : args to abc                      e.g. "-D 2000" (delay target ps), "-fast"
      abc_script : full abc script string (overrides abc_args), e.g. "+strash;dch;map;topo;stime -p"
      post       : yosys cmds after abc             e.g. ["opt_clean -purge"]
    """
    recipe = _norm_recipe(recipe)
    L = _lib(lib)
    dont_use = " ".join(f"-dont_use {d}" for d in SKY130_DONT_USE) if "sky130" in L else ""
    abc = f"abc -liberty {L} {dont_use} "
    if recipe.get("abc_script"):
        sc = recipe["abc_script"].strip()
        if sc.startswith("+"):          # inline form: +cmd1;cmd2 -D 1400  (spaces -> commas for yosys)
            abc += "-script " + sc.replace(" ", ",")
        else:
            Path(cwd, "abc.script").write_text(sc.replace(";", "\n") + "\n")
            abc += "-script abc.script"
    else:
        abc += recipe.get("abc_args", "")
    script = [f"read_verilog -sv {' '.join(map(str, files))}",
              f"hierarchy -check -top {top}",
              f"synth -top {top} -flatten {recipe.get('synth_args', '')}",
              *recipe.get("pre", []),
              "tee -q -o stat_pre.json stat -json",
              "check",
              f"dfflibmap -liberty {L} {dont_use}",
              abc,
              *recipe.get("post", []),
              "opt_clean",
              "hilomap -singleton -hicell sky130_fd_sc_hd__conb_1 HI -locell sky130_fd_sc_hd__conb_1 LO" if "sky130" in L else "",
              f"tee -q -o stat_post.json stat -json -liberty {L}",
              f"write_verilog -noattr -noexpr -nohex -nodec {netlist}"]
    Path(cwd, "synth.ys").write_text("\n".join(s for s in script if s) + "\n")
    rc, out, sec = _run(["yosys", "-l", "synth.log", "-q", "-s", "synth.ys"], cwd, timeout)
    log = Path(cwd, "synth.log").read_text(errors="ignore") if Path(cwd, "synth.log").exists() else out
    res = {"tool": "yosys_synth", "ok": rc == 0, "metrics": {}, "log": _tail(out if rc else log, 30),
           "seconds": sec, "netlist": str(Path(cwd, netlist)), "recipe": recipe}
    if rc != 0:
        return res
    pre = json.loads(Path(cwd, "stat_pre.json").read_text())["design"]["num_cells_by_type"]
    post = json.loads(Path(cwd, "stat_post.json").read_text())["design"]
    flops = sum(v for k, v in pre.items() if re.match(r"\$_(S?DFF|DFFE|SDFFE|SDFFCE|ALDFF|DFFSR)", k))
    latches = sum(v for k, v in pre.items() if "DLATCH" in k)
    res["metrics"] = {
        "area": round(post.get("area", 0.0), 3),
        "seq_area": round(post.get("sequential_area", 0.0), 3),
        "cells": post["num_cells"], "flops": flops, "latches": latches,
        "latch_warnings": log.count("Latch inferred"),
        "logic_loops": len(re.findall(r"found logic loop", log)),
        "multi_driven": len(re.findall(r"multiple conflicting drivers|is used but has no driver", log)),
        "cells_by_type": post.get("num_cells_by_type", {}),
    }
    return res


# --------------------------------------------------------------------------- sta
def sta(netlist, top, cwd=".", lib="sky130hd", period_ns=5.0, clock_port="clk",
        io_delay_frac=0.0, timeout=60):
    """OpenSTA on a yosys netlist. Uses a virtual clock if `clock_port` is absent (pure comb)."""
    L = _lib(lib)
    has_clk = bool(re.search(rf"\binput\s+{clock_port}\b", Path(cwd, netlist).read_text()))
    clk = (f"create_clock -name clk -period {period_ns} [get_ports {clock_port}]\n"
           f"set ins [delete_from_list [all_inputs] [get_ports {clock_port}]]\n") if has_clk else \
          (f"create_clock -name clk -period {period_ns}\nset ins [all_inputs]\n")
    d = period_ns * io_delay_frac
    tcl = (f"read_liberty {L}\nread_verilog {netlist}\nlink_design {top}\n{clk}"
           f"set_input_delay {d} -clock clk $ins\nset_output_delay {d} -clock clk [all_outputs]\n"
           "set_load 0.005 [all_outputs]\n"
           'puts "RESULT_WNS [sta::format_time [sta::worst_slack_cmd max] 4]"\n'
           'puts "RESULT_TNS [sta::format_time [sta::total_negative_slack_cmd max] 4]"\n'
           'puts "RESULT_HOLD [sta::format_time [sta::worst_slack_cmd min] 4]"\n'
           "report_checks -path_delay max -digits 3 -fields {fanout}\n")
    Path(cwd, "sta.tcl").write_text(tcl)
    rc, out, sec = _run(["sta", "-no_splash", "-exit", "sta.tcl"], cwd, timeout)
    g = lambda k: (float(m.group(1)) if (m := re.search(rf"{k}\s+(-?[\d.]+|INF)", out)) and m.group(1) != "INF" else None)
    wns, tns, hold = g("RESULT_WNS"), g("RESULT_TNS"), g("RESULT_HOLD")
    crit = None
    if wns is not None:
        crit = round(period_ns - wns, 4)
    return {"tool": "opensta", "ok": rc == 0 and wns is not None and wns >= 0,
            "metrics": {"wns": wns, "tns": tns, "hold_wns": hold, "period_ns": period_ns,
                        "critical_path_ns": crit, "fmax_mhz": round(1000 / crit, 1) if crit and crit > 0 else None,
                        "virtual_clock": not has_clk},
            "log": _tail(out, 45), "seconds": sec}


# --------------------------------------------------------------------------- iverilog sim
def sim_iverilog(files, tb_top, cwd=".", defines=None, params=None, timeout=60):
    """
    Compile + run with Icarus.  Testbench convention: print `RESULT <passed> <total>`
    at the end, and `ERROR: ...` per mismatch.  Also flags X/Z on checked outputs if tb prints `XPROP`.
    """
    cmd = ["iverilog", "-g2012", "-Wall", "-Wno-timescale", "-o", "sim.vvp", "-s", tb_top]
    cmd += [f"-D{d}" for d in (defines or [])] + [f"-P{tb_top}.{k}={v}" for k, v in (params or {}).items()]
    rc, out, sec = _run(cmd + list(map(str, files)), cwd, timeout)
    if rc != 0:
        return {"tool": "iverilog", "ok": False, "metrics": {"compile_error": True, "passed": 0, "total": 1},
                "log": _tail(out), "seconds": sec}
    rc, out2, sec2 = _run(["vvp", "-n", "sim.vvp"], cwd, timeout)
    m = re.findall(r"RESULT\s+(\d+)\s+(\d+)", out2)
    passed, total = (int(m[-1][0]), int(m[-1][1])) if m else (0, 1)
    errs = [l for l in out2.splitlines() if l.startswith("ERROR")]
    return {"tool": "iverilog", "ok": bool(m) and passed == total and total > 0 and "TIMEOUT" not in out2,
            "metrics": {"compile_error": False, "passed": passed, "total": total,
                        "timeout": "TIMEOUT" in out2 or not m, "xprop": out2.count("XPROP")},
            "first_errors": errs[:8], "log": _tail(out2), "seconds": round(sec + sec2, 3)}


# --------------------------------------------------------------------------- cocotb
def sim_cocotb(files, toplevel, test_module, test_dir, cwd=".", simulator="icarus", timeout=120,
               parameters=None):
    """Run a cocotb test module (Python) against the RTL. Done in a subprocess for isolation."""
    code = f"""
import sys, json
from cocotb_tools.runner import get_runner, get_results
r = get_runner({simulator!r})
r.build(sources={[str(Path(f).resolve()) for f in files]!r}, hdl_toplevel={toplevel!r}, build_dir='cocotb_build',
        always=True, timescale=('1ns','1ps'), parameters={parameters or {}!r})
xml = r.test(hdl_toplevel={toplevel!r}, test_module={test_module!r}, test_dir={str(Path(test_dir).resolve())!r},
             build_dir='cocotb_build', results_xml='results.xml', timescale=('1ns','1ps'))
n, f = get_results(xml)
print('COCOTB_RESULT', n - f, n)
"""
    env_py = shutil.which("python") or "python3"
    rc, out, sec = _run([env_py, "-c", code], cwd, timeout)
    m = re.search(r"COCOTB_RESULT\s+(\d+)\s+(\d+)", out)
    passed, total = (int(m.group(1)), int(m.group(2))) if m else (0, 1)
    fails = [l.strip() for l in out.splitlines() if "FAIL" in l and "test_" in l][:8]
    return {"tool": "cocotb", "ok": bool(m) and passed == total and total > 0,
            "metrics": {"passed": passed, "total": total, "crashed": not m},
            "first_errors": fails, "log": _tail(out, 50), "seconds": sec}


# --------------------------------------------------------------------------- formal
def formal(files, top, cwd=".", mode="prove", depth=20, engine=None, name="formal", timeout=180):
    """
    SymbiYosys.  `files` must include a formal wrapper with `assert property`/`assume`/`cover`.
    mode: prove (k-induction / pdr) | bmc | cover.
    """
    engine = engine or {"prove": "abc pdr", "bmc": "smtbmc bitwuzla", "cover": "smtbmc bitwuzla"}[mode]
    names = [Path(f).name for f in files]
    sby = (f"[options]\nmode {mode}\ndepth {depth}\n\n[engines]\n{engine}\n\n[script]\n"
           f"read -formal {' '.join(names)}\nprep -top {top}\n\n[files]\n" +
           "\n".join(str(Path(f).resolve()) for f in files) + "\n")
    Path(cwd, f"{name}.sby").write_text(sby)
    rc, out, sec = _run(["sby", "-f", f"{name}.sby"], cwd, timeout)
    status = (m.group(1) if (m := re.search(r"DONE \((\w+)", out)) else ("TIMEOUT" if rc == -9 else "ERROR"))
    failed = re.findall(r"Assert failed in (\S+): (\S+)", out) + re.findall(r"(?:Assert|Assertion) .*failed.*?(\S+\.sv:\d+)", out)
    reached = len(re.findall(r"Reached cover statement", out))
    unreached = len(re.findall(r"Unreached cover statement", out))
    trace = None
    for p in sorted(Path(cwd, name).rglob("trace*.vcd")) if Path(cwd, name).exists() else []:
        trace = str(p)
        break
    return {"tool": "sby", "ok": status == "PASS",
            "metrics": {"status": status, "failed_asserts": len(failed), "cover_reached": reached,
                        "cover_unreached": unreached, "mode": mode, "depth": depth},
            "failed": [" ".join(f) for f in failed][:8], "trace": trace,
            "log": _tail(out, 30), "seconds": sec}


# --------------------------------------------------------------------------- equivalence
def equiv(gold_files, rev_files, top, cwd=".", seq_depth=0, timeout=120):
    """
    Yosys miter + SAT equivalence.  seq_depth=0 -> combinational proof;
    seq_depth=N -> bounded sequential equivalence from all-zero init for N cycles.
    """
    rd = lambda fs: f"read_verilog -sv {' '.join(str(Path(f).resolve()) for f in fs)}"
    prep = f"hierarchy -top {top}; proc; flatten; hierarchy -top {top}; opt_clean; memory; opt -fast"
    sat = ("sat -verify -prove trigger 0 -show-inputs -show-outputs miter" if seq_depth == 0 else
           f"sat -verify -prove trigger 0 -set-init-zero -seq {seq_depth} -show-inputs miter")
    script = (f"{rd(gold_files)}; {prep}; rename {top} gold; design -stash g; "
              f"{rd(rev_files)}; {prep}; rename {top} rev; design -stash r; "
              f"design -copy-from g -as gold gold; design -copy-from r -as rev rev; "
              f"miter -equiv -flatten -ignore_gold_x gold rev miter; hierarchy -top miter; {sat}")
    rc, out, sec = _run(["yosys", "-p", script], cwd, timeout)
    proven = rc == 0 and ("SAT proof finished - no model found: SUCCESS" in out or "QED" in out)
    return {"tool": "yosys_equiv", "ok": proven,
            "metrics": {"equivalent": proven, "seq_depth": seq_depth,
                        "counterexample": (not proven) and "model found" in out},
            "log": _tail(out, 40), "seconds": sec}


# --------------------------------------------------------------------------- mock openroad
def openroad_mock(synth_res, sta_res, target_util=0.55):
    """
    Deterministic stand-in for OpenROAD place&route (terminal leaf only).
    Post-route slack degrades with design size (wire load), DRC grows with util.
    """
    a = synth_res["metrics"]["area"]
    cells = synth_res["metrics"]["cells"]
    wns = sta_res["metrics"]["wns"] or 0.0
    die = a / target_util
    wire_penalty = 0.012 * math.sqrt(max(cells, 1)) * (1 + 0.5 * max(0, target_util - 0.5))
    post_wns = round(wns - wire_penalty, 4)
    drc = 0 if target_util <= 0.7 else int((target_util - 0.7) * cells * 0.5)
    return {"tool": "openroad_mock", "ok": post_wns >= 0 and drc == 0,
            "metrics": {"die_area_um2": round(die, 1), "core_util": target_util,
                        "post_route_wns": post_wns, "est_wirelength_um": round(2.1 * cells * math.sqrt(die) / 10, 1),
                        "drc_violations": drc},
            "log": "mocked OpenROAD (floorplan->place->cts->route estimates)", "seconds": 0.0}


# --------------------------------------------------------------------------- gate-level sim
def gls(files, top, tb_files, tb_top, cwd=".", params=None, timeout=90):
    """Synthesize to yosys generic gates, then re-run the RTL testbench on the netlist (catches sim/synth mismatch)."""
    rd = " ".join(str(Path(f).resolve()) for f in files)
    chp = "".join(f"chparam -set {k} {v} {top}; " for k, v in (params or {}).items())
    rc, out, sec = _run(["yosys", "-q", "-p", f"read_verilog -sv {rd}; hierarchy -top {top}; {chp}synth -flatten -top {top}; "
                         f"rename -enumerate; write_verilog -noattr gl_{top}.v"], cwd, timeout)
    if rc != 0:
        return {"tool": "gls", "ok": False, "metrics": {"compile_error": True, "passed": 0, "total": 1},
                "log": _tail(out), "seconds": sec}
    dat = subprocess.run(["yosys-config", "--datdir"], capture_output=True, text=True).stdout.strip()
    r = sim_iverilog([f"gl_{top}.v", f"{dat}/simcells.v", f"{dat}/simlib.v", *tb_files], tb_top, cwd=cwd,
                     params=params, defines=["GLS"], timeout=timeout)
    r["tool"] = "gls"
    r["seconds"] = round(r["seconds"] + sec, 3)
    return r


# --------------------------------------------------------------------------- word-level equivalence (SMT)
def _ports(files, top, cwd):
    rd = " ".join(str(Path(f).resolve()) for f in files)
    rc, out, _ = _run(["yosys", "-q", "-p", f"read_verilog -sv {rd}; hierarchy -top {top}; proc; write_json _ports.json"], cwd, 60)
    if rc != 0:
        raise RuntimeError(out[-800:])
    mods = json.loads(Path(cwd, "_ports.json").read_text())["modules"]
    m = mods.get(top) or next(v for k, v in mods.items() if k.split("\\")[-1] == top)
    return {n: (p["direction"], len(p["bits"])) for n, p in m["ports"].items()}


def equiv_smt(gold_files, rev_files, top, cwd=".", depth=1, timeout=120, split=None):
    """
    Miter proof with SymbiYosys + bitwuzla (word-level).
    Combinational designs: depth=1. Ports must match by name and width.
    split=[input names]: case-split on small select inputs (each value proven separately with the
    input tied to a constant, so yosys const-folds muxes; makes datapath-sharing proofs trivial).
    """
    if split:
        import itertools
        gp = _ports(gold_files, top, cwd)
        widths = [gp[n][1] for n in split]
        total_sec, logs = 0.0, []
        for combo in itertools.product(*[range(2 ** w) for w in widths]):
            sub = Path(cwd, "split_" + "_".join(map(str, combo))); sub.mkdir(exist_ok=True)
            r = _equiv_smt_one(gold_files, rev_files, top, sub, depth, timeout, dict(zip(split, combo)))
            total_sec += r["seconds"] or 0
            if not r["ok"]:
                r["metrics"]["failing_case"] = dict(zip(split, combo))
                r["seconds"] = round(total_sec, 3)
                return r
            logs.append(f"case {dict(zip(split, combo))}: PASS")
        return {"tool": "equiv_smt", "ok": True, "metrics": {"equivalent": True, "status": "PASS", "cases": len(logs)},
                "log": "\n".join(logs), "seconds": round(total_sec, 3)}
    return _equiv_smt_one(gold_files, rev_files, top, cwd, depth, timeout, {})


def _equiv_smt_one(gold_files, rev_files, top, cwd, depth, timeout, consts):
    try:
        gp, rp = _ports(gold_files, top, cwd), _ports(rev_files, top, cwd)
    except Exception as e:
        return {"tool": "equiv_smt", "ok": False, "metrics": {"equivalent": False, "port_mismatch": False},
                "log": f"parse error: {e}", "seconds": 0}
    # combinational loops make miter semantics meaningless (solver picks loop values) -> reject first
    rd = " ".join(str(Path(f).resolve()) for f in rev_files)
    _, lo, _ = _run(["yosys", "-p", f"read_verilog -sv {rd}; hierarchy -top {top}; proc; flatten; opt_clean; check"], cwd, 60)
    loops = len(re.findall(r"found logic loop", lo))
    if loops:
        return {"tool": "equiv_smt", "ok": False, "metrics": {"equivalent": False, "logic_loops": loops},
                "log": "revised design has combinational loops; equivalence undefined", "seconds": 0}
    if gp != rp:
        return {"tool": "equiv_smt", "ok": False, "metrics": {"equivalent": False, "port_mismatch": True},
                "log": f"port mismatch gold={gp} rev={rp}", "seconds": 0}
    ins = [(n, w) for n, (d, w) in gp.items() if d == "input"]
    outs = [(n, w) for n, (d, w) in gp.items() if d == "output"]
    decl = lambda w: f"[{w-1}:0] " if w > 1 else ""
    miter = ["module __miter ("] + [",\n".join(f"  input wire {decl(w)}{n}" for n, w in ins if n not in consts)] + [");"]
    miter += [f"  wire {decl(gp[n][1])}{n} = {gp[n][1]}'d{v};" for n, v in consts.items()]
    miter += [f"  wire {decl(w)}g_{n}, r_{n};" for n, w in outs]
    conn = lambda pre: ", ".join([f".{n}({n})" for n, _ in ins] + [f".{n}({pre}_{n})" for n, _ in outs])
    miter += [f"  __gold_{top} g ({conn('g')});", f"  {top} r ({conn('r')});",
              "  always @(*) begin"] + [f"    assert (g_{n} == r_{n});" for n, _ in outs] + ["  end", "endmodule"]
    Path(cwd, "__miter.sv").write_text("\n".join(miter) + "\n")
    # always stage gold sources under a prefix so basenames never collide with the candidate's
    gdir = Path(cwd, "_gold_src"); gdir.mkdir(exist_ok=True)
    gf = []
    for f in gold_files:
        dst = gdir / ("gold__" + Path(f).name); shutil.copy(f, dst); gf.append(str(dst.resolve()))
    rf = [str(Path(f).resolve()) for f in rev_files]
    sby = ("[options]\nmode bmc\ndepth {d}\n\n[engines]\nsmtbmc bitwuzla\n\n[script]\n"
           "read -sv {g}\nhierarchy -top {t}\nrename {t} __gold_{t}\nread -sv {r}\nread -formal __miter.sv\nprep -flatten -top __miter\nopt -full\n\n[files]\n{files}\n"
           ).format(d=depth, t=top, g=" ".join(Path(f).name for f in gf), r=" ".join(Path(f).name for f in rf),
                    files="\n".join(gf + rf + [str(Path(cwd, "__miter.sv").resolve())]))
    Path(cwd, "equiv.sby").write_text(sby)
    rc, out, sec = _run(["sby", "-f", "equiv.sby"], cwd, timeout)
    status = (m.group(1) if (m := re.search(r"DONE \((\w+)", out)) else ("TIMEOUT" if rc == -9 else "ERROR"))
    return {"tool": "equiv_smt", "ok": status == "PASS",
            "metrics": {"equivalent": status == "PASS", "status": status, "counterexample": status == "FAIL"},
            "log": _tail(out, 25), "seconds": sec}
