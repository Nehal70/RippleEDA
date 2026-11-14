"""
Agent-facing sandbox: an editable copy of a task's start/ plus EDA tools exposed as
OpenAI-style function tools. Everything here is "public": it only uses files in the
workspace (the agent's own edits / tests) and the task's public prompt + targets.
Hidden grader assets (tb/, formal/, mutants/, solution/) are never touched.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path

import yaml

from eda import tools

MAX_OUT = 3000  # chars of tool output returned to the model


def _clip(s, n=MAX_OUT):
    s = str(s)
    return s if len(s) <= n else s[: n // 2] + "\n...[truncated]...\n" + s[-n // 2:]


TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "list_files", "description": "List files in the workspace.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read a workspace file.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "Create or overwrite a workspace file with the full new content.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                       "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "run_lint", "description": "Verilator --lint-only -Wall on the design RTL.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "run_synth",
        "description": "Yosys+ABC synthesis to sky130hd. Reports area, cells, flops, latches, logic loops. "
                       "Uses recipe.json in the workspace if present ({synth_args, pre, abc_args, abc_script}).",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "run_sta", "description": "Synthesize then run OpenSTA at the given clock period (ns). Reports WNS/critical path.",
        "parameters": {"type": "object", "properties": {"period_ns": {"type": "number"},
                                                        "clock": {"type": "string", "description": "clock port, default clk"}},
                       "required": ["period_ns"]}}},
    {"type": "function", "function": {
        "name": "run_sim",
        "description": "Compile and run a SystemVerilog testbench with Icarus (4-state). The testbench should print "
                       "'RESULT <passed> <total>' and 'ERROR: ...' lines. Design RTL is included automatically.",
        "parameters": {"type": "object", "properties": {
            "tb_files": {"type": "array", "items": {"type": "string"}}, "top": {"type": "string"},
            "params": {"type": "object", "description": "testbench parameter overrides"}},
            "required": ["tb_files", "top"]}}},
    {"type": "function", "function": {
        "name": "run_gls",
        "description": "Gate-level simulation: synthesize the design with yosys (applying params via chparam) and run "
                       "your testbench on the netlist. Catches RTL-vs-synthesis mismatches.",
        "parameters": {"type": "object", "properties": {
            "tb_files": {"type": "array", "items": {"type": "string"}}, "top": {"type": "string"},
            "params": {"type": "object", "description": "design/testbench parameter overrides"}},
            "required": ["tb_files", "top"]}}},
    {"type": "function", "function": {
        "name": "run_formal",
        "description": "SymbiYosys on a formal harness file you wrote (immediate assert/assume/cover in always blocks). "
                       "mode: prove | bmc | cover. Design RTL is included automatically. Returns status, failing asserts, "
                       "and the counterexample/cover witness path.",
        "parameters": {"type": "object", "properties": {
            "files": {"type": "array", "items": {"type": "string"}}, "top": {"type": "string"},
            "mode": {"type": "string", "enum": ["prove", "bmc", "cover"]}, "depth": {"type": "integer"}},
            "required": ["files", "top"]}}},
    {"type": "function", "function": {
        "name": "run_cocotb", "description": "Run a cocotb test module (python file in the workspace) against the RTL with Icarus.",
        "parameters": {"type": "object", "properties": {"module": {"type": "string", "description": "python module name, e.g. test_axil"},
                                                        "dir": {"type": "string", "description": "workspace dir containing it"}},
                       "required": ["module", "dir"]}}},
    {"type": "function", "function": {
        "name": "check_equiv",
        "description": "Formal equivalence of the current RTL against the ORIGINAL RTL you were given (word-level SMT). "
                       "Optional split: list of small select inputs to case-split on.",
        "parameters": {"type": "object", "properties": {"split": {"type": "array", "items": {"type": "string"}}}}}},
    {"type": "function", "function": {
        "name": "run_shell", "description": "Run a short shell command inside the workspace (python3, cat, grep, sed...). 60 s limit.",
        "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}}},
    {"type": "function", "function": {
        "name": "submit", "description": "Declare the task finished. The workspace is then graded by the hidden signoff suite.",
        "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}}}},
]


class Workspace:
    """A task workspace. Cheap to fork (directory copy) for tree search."""

    def __init__(self, task_dir, root=None, _clone_from=None):
        self.task_dir = Path(task_dir).resolve()
        self.spec = yaml.safe_load((self.task_dir / "task.yaml").read_text())
        self.root = Path(root or tempfile.mkdtemp(prefix=f"ws_{self.spec['id']}_"))
        if _clone_from is None:
            shutil.copytree(self.task_dir / "start", self.root, dirs_exist_ok=True)
        self.orig = self.task_dir / "start"
        self.history = []          # agent-run checks: [{tool, ok, score, metrics}] (public evidence for the PRM)
        self.submitted = False

    # ----------------------------------------------------------------- forking
    def fork(self):
        new_root = Path(tempfile.mkdtemp(prefix=f"ws_{self.spec['id']}_"))
        shutil.copytree(self.root, new_root, dirs_exist_ok=True)
        w = Workspace(self.task_dir, new_root, _clone_from=self)
        w.history = copy.deepcopy(self.history)
        w.submitted = self.submitted
        return w

    def digest(self):
        h = hashlib.sha1()
        for p in sorted(self.root.rglob("*")):
            if p.is_file() and not any(x.startswith((".", "_run")) for x in p.relative_to(self.root).parts):
                h.update(str(p.relative_to(self.root)).encode()); h.update(p.read_bytes())
        return h.hexdigest()[:12]

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)

    # ----------------------------------------------------------------- helpers
    def rtl(self):
        return [str(self.root / f) for f in self.spec["rtl"]]

    def _p(self, rel):
        p = (self.root / rel).resolve()
        if not str(p).startswith(str(self.root.resolve())):
            raise ValueError("path escapes workspace")
        return p

    def _scratch(self, name):
        d = self.root / f"_run_{name}"
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir()
        return d

    def recipe(self):
        f = self.root / "recipe.json"
        try:
            return json.loads(f.read_text()) if f.exists() else None
        except Exception:
            return None

    def _record(self, tool, ok, score, metrics):
        self.history.append({"tool": tool, "ok": bool(ok), "score": float(score), "metrics": metrics})

    # ----------------------------------------------------------------- dispatch
    def call(self, name, args):
        try:
            return getattr(self, "t_" + name)(**(args or {}))
        except TypeError as e:
            return f"bad arguments for {name}: {e}"
        except Exception as e:  # tools must never crash the agent loop
            return f"{name} failed: {e!r}"

    def t_list_files(self):
        return "\n".join(str(p.relative_to(self.root)) for p in sorted(self.root.rglob("*"))
                         if p.is_file() and not any(x.startswith("_run") for x in p.relative_to(self.root).parts))

    def t_read_file(self, path):
        return _clip(self._p(path).read_text(errors="ignore"), 12000)

    def t_write_file(self, path, content):
        p = self._p(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"wrote {path} ({len(content)} bytes)"

    def t_run_lint(self):
        r = tools.lint(self.rtl(), self.spec["top"], cwd=self._scratch("lint"))
        m = r["metrics"]
        self._record("lint", r["ok"], 0.0 if m["errors"] else max(0, 1 - 0.15 * m["warnings"]), m)
        return _clip(f"errors={m['errors']} warnings={m['warnings']} by_code={m['by_code']}\n" + "\n".join(r["messages"][:25]))

    def _synth(self):
        r = tools.synth(self.rtl(), self.spec["top"], cwd=self._scratch("synth"), recipe=self.recipe())
        if r["ok"]:
            m = r["metrics"]
            self._record("synth", m["latches"] == 0 and m["logic_loops"] == 0, 1.0, {k: m[k] for k in ("area", "cells", "flops", "latches", "logic_loops")})
        else:
            self._record("synth", False, 0.0, {})
        return r

    def t_run_synth(self):
        r = self._synth()
        if not r["ok"]:
            return "synthesis FAILED:\n" + _clip(r["log"])
        m = r["metrics"]
        return (f"area={m['area']} um^2 cells={m['cells']} flops={m['flops']} latches={m['latches']} "
                f"logic_loops={m['logic_loops']} recipe={self.recipe()}")

    def t_run_sta(self, period_ns, clock="clk"):
        s = self._synth()
        if not s["ok"]:
            return "synthesis FAILED:\n" + _clip(s["log"])
        r = tools.sta(s["netlist"], self.spec["top"], cwd=s["netlist"].rsplit("/", 1)[0], period_ns=float(period_ns), clock_port=clock)
        m = r["metrics"]
        w = m["wns"]
        self._record("sta", r["ok"], 0.0 if w is None else (1.0 if w >= 0 else max(0, 1 + w / float(period_ns))),
                     {"wns": w, "period": period_ns, "area": s["metrics"]["area"]})
        return _clip(f"WNS={w} ns critical_path={m['critical_path_ns']} ns (period {period_ns}) area={s['metrics']['area']}\n"
                     + "\n".join(r["log"].splitlines()[-30:]), 2500)

    def t_run_sim(self, tb_files, top, params=None):
        r = tools.sim_iverilog(self.rtl() + [str(self._p(f)) for f in tb_files], top, cwd=self._scratch("sim"), params=params)
        m = r["metrics"]
        self._record("sim:" + ",".join(tb_files), r["ok"], m["passed"] / max(1, m["total"]), m)
        return _clip(f"passed={m['passed']}/{m['total']} compile_error={m['compile_error']}\n"
                     + "\n".join(r.get("first_errors", [])) + "\n--- log tail ---\n" + r["log"])

    def t_run_gls(self, tb_files, top, params=None):
        r = tools.gls(self.rtl(), self.spec["top"], [str(self._p(f)) for f in tb_files], top, cwd=self._scratch("gls"),
                      params=params)
        m = r["metrics"]
        self._record("sim:gls:" + ",".join(tb_files), r["ok"], m["passed"] / max(1, m["total"]), m)
        return _clip(f"gate-level passed={m['passed']}/{m['total']} compile_error={m['compile_error']}\n"
                     + "\n".join(r.get("first_errors", [])) + "\n--- log tail ---\n" + r["log"])

    def t_run_formal(self, files, top, mode="prove", depth=20):
        r = tools.formal(self.rtl() + [str(self._p(f)) for f in files], top, cwd=self._scratch("formal"), mode=mode, depth=int(depth))
        m = r["metrics"]
        self._record(f"formal:{mode}:" + ",".join(files), r["ok"], 1.0 if r["ok"] else 0.0, m)
        out = f"status={m['status']} failed={r['failed']} cover_reached={m['cover_reached']} cover_unreached={m['cover_unreached']}"
        if r["trace"]:
            tr = Path(r["trace"])
            keep = self.root / "_witness"
            keep.mkdir(exist_ok=True)
            for f in tr.parent.glob(tr.stem + ".*"):
                shutil.copy(f, keep / f.name)
            out += f"\ntrace files copied to _witness/{tr.stem}.vcd and _witness/{tr.stem}.yw"
        return _clip(out + "\n" + r["log"])

    def t_run_cocotb(self, module, dir):
        r = tools.sim_cocotb(self.rtl(), self.spec["top"], module, self._p(dir), cwd=self._scratch("cocotb"))
        m = r["metrics"]
        self._record(f"cocotb:{module}", r["ok"], m["passed"] / max(1, m["total"]), m)
        return _clip(f"passed={m['passed']}/{m['total']}\n" + "\n".join(r.get("first_errors", [])) + "\n" + r["log"])

    def t_check_equiv(self, split=None):
        gold = [str(self.orig / f) for f in self.spec["rtl"]]
        r = tools.equiv_smt(gold, self.rtl(), self.spec["top"], cwd=self._scratch("equiv"), split=split)
        self._record("equiv", r["ok"], 1.0 if r["ok"] else 0.0, r["metrics"])
        return _clip(f"equivalent={r['ok']} {r['metrics']}\n" + r["log"], 2000)

    def t_run_shell(self, cmd):
        import subprocess
        try:
            p = subprocess.run(cmd, shell=True, cwd=self.root, capture_output=True, text=True, timeout=60)
            return _clip(p.stdout + p.stderr) or "(no output)"
        except subprocess.TimeoutExpired:
            return "timeout"

    def t_submit(self, summary=""):
        self.submitted = True
        return "submitted"


def public_targets(spec):
    """Numeric targets that are stated in the public prompt (period, area, flops)."""
    t = {}
    for c in spec["checks"]:
        if c["type"] == "sta":
            t["period_ns"], t["clock"] = c["period_ns"], c.get("clock", "clk")
        if c["type"] == "synth":
            for k in ("max_area", "max_cells", "max_flops"):
                if k in c:
                    t[k] = c[k]
    p = spec["prompt"]
    t["behaviour_preserving"] = bool(re.search(r"without changing its\s+behaviou?r", p))
    return t
