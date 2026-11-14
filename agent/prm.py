"""
Process reward model for RTL/DV agent steps.

mode="honest" (the headline result) — uses ONLY information the agent itself could obtain:
    exec part  : lint, synthesis (latches / loops / area / flop budget from the prompt), STA at the
                 prompt's period, equivalence to the ORIGINAL RTL when the prompt says behaviour must
                 not change, and the agent's own tests / formal runs recorded in the workspace.
    judge part : generative PRM — Ripple is asked whether the trajectory is on track; score = P(Yes)
                 from first-token logprobs (GenPRM / ThinkPRM-style "LLM-as-PRM").
    value = 0.7 * exec + 0.3 * judge

mode="oracle" (upper bound only, NOT a fair comparison) — value = hidden grader reward.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from eda import tools
from eda.grade import grade
from agent.workspace import public_targets

JUDGE_TMPL = """You are reviewing an RTL/DV engineer's work in progress.

TASK:
{prompt}

LATEST ACTIONS AND TOOL RESULTS:
{recent}

CURRENT DESIGN FILE(S) (truncated):
{rtl}

Question: Is this work on track to fully satisfy the task (functionally correct under all legal
conditions, signoff-clean, and meeting every stated target)? Answer with exactly one word: Yes or No."""


class PRM:
    def __init__(self, mode="honest", client=None, w_exec=0.7, jury=None, probe=True, cjudge=None):
        assert mode in ("honest", "oracle")
        self.mode, self.client, self.w_exec, self.jury, self.probe_on = mode, client, w_exec, jury, probe
        self.cjudge = cjudge          # core.calibrated_judge.CalibratedJudge (pointwise, calibrated)
        self.cache, self.lock = {}, threading.Lock()

    # ------------------------------------------------------------------ v2: listwise jury over siblings
    def value_batch(self, parent_ws, parent_det, parent_info, children):
        """children: list of (ws, messages, info). Returns [(value, det)] using the jury (judge v2):
        value = 0.55*exec + 0.30*promise + 0.15*progress, progress = jury progress + exec delta vs parent."""
        from concurrent.futures import ThreadPoolExecutor
        from agent.judge import describe, rtl_diff, _clip
        if self.mode == "oracle" or (self.jury is None and self.cjudge is None):
            return [self.value(w, m) for w, m, _ in children]
        if self.cjudge is not None:
            return self._value_batch_calibrated(parent_ws, parent_det, parent_info, children)
        try:
            with ThreadPoolExecutor(max_workers=len(children)) as ex:
                execs = list(ex.map(lambda c: self.exec_score(c[0]), children))
            e_par = parent_det.get("exec") if parent_det and "exec" in parent_det else self.exec_score(parent_ws)[0]
            rtl = "\n\n".join(_clip((parent_ws.root / f).read_text(errors="ignore"), 2500)
                               for f in parent_ws.spec["rtl"] if (parent_ws.root / f).exists())
            parent_desc = describe(parent_info, parent_det) + "\nCURRENT RTL:\n" + rtl
            cands = [describe(info, det) + "\nRTL CHANGE vs parent:\n" + rtl_diff(parent_ws, ws, ws.spec["rtl"])
                     for (ws, _, info), (_, det) in zip(children, execs)]
            jr = self.jury.score(parent_ws.spec, parent_desc, cands)
        except Exception as e:
            return [(0.0, {"prm_error": repr(e)[:200]}) for _ in children]
        out = []
        for (e, det), (promise, prog) in zip(execs, jr):
            d = dict(det)
            delta = max(-1.0, min(1.0, 4 * (e - e_par)))
            progress = max(0.0, min(1.0, 0.5 + 0.25 * prog + 0.25 * delta))
            v = 0.55 * e + 0.30 * promise + 0.15 * progress
            d.update(exec=round(e, 4), promise=round(promise, 3), jprogress=round(prog, 2), delta=round(e - e_par, 3))
            out.append((v, d))
        # judge-by-doing, DIFFERENTIAL (CodeT-style agreement + PAV-style progress): the judge writes one falsifying
        # testbench aimed at the most promising child, and we run it on the parent and on EVERY sibling. LLM-written
        # tests are often wrong themselves (measured: probes failed the reference solution of T05), so a probe only
        # counts when it discriminates: fail where the parent or a sibling passes = regression (-0.15);
        # pass where the parent failed = verified fix (+0.10); same outcome everywhere = uninformative (ignored).
        if self.probe_on and parent_ws.spec.get("category") == "rtl-repair" and out:
            i = max(range(len(out)), key=lambda k: out[k][0])
            code = self.jury.probe_code(children[i][0], {k: v for k, v in out[i][1].items() if k != "judge"})
            if code:
                par, _ = self.jury.run_probe(parent_ws, code, "probe_parent")
                res = [self.jury.run_probe(ws, code, "probe") for ws, _, _ in children]
                valid = [r for r, _ in res if r != "invalid"] + ([par] if par != "invalid" else [])
                informative = "pass" in valid and "fail" in valid
                for k, (st, detail) in enumerate(res):
                    v, d = out[k]
                    d = {**d, "probe": st, "probe_parent": par, "probe_informative": informative, "probe_detail": detail[:160]}
                    if informative and st == "fail" and ("pass" in [r for r, _ in res] or par == "pass"):
                        v -= 0.15
                    elif informative and st == "pass" and par == "fail":
                        v += 0.10
                    out[k] = (max(0.0, min(1.0, v)), d)
        return out

    # ------------------------------------------------------------------ exec (public) part
    def exec_score(self, ws):
        key = (ws.spec["id"], ws.digest())
        with self.lock:
            if key in self.cache:
                return self.cache[key]
        t = public_targets(ws.spec)
        parts, det = [], {}
        cwd = ws._scratch("prm")
        rtl = ws.rtl()
        L = tools.lint(rtl, ws.spec["top"], cwd=cwd)
        m = L["metrics"]
        s_lint = 0.0 if m["errors"] else max(0.0, 1 - 0.15 * m["warnings"])
        parts.append((s_lint, 1.0)); det["lint"] = round(s_lint, 3)
        if m["errors"]:
            res = (0.0, det)                          # does not even parse
        else:
            S = tools.synth(rtl, ws.spec["top"], cwd=cwd, recipe=ws.recipe())
            if not S["ok"]:
                parts.append((0.0, 2.0)); det["synth"] = 0.0
            else:
                sm = S["metrics"]; s = 1.0
                if sm["latches"] or sm["latch_warnings"]: s -= 0.5
                if sm["logic_loops"]: s -= 0.5
                for k, met in (("max_area", "area"), ("max_flops", "flops"), ("max_cells", "cells")):
                    if k in t and sm[met] > t[k]:
                        s = min(s, (t[k] / sm[met]) ** 3)   # steep: 12% over budget ~ 0.7
                parts.append((max(0.0, s), 4.0 if any(k in t for k in ('max_area','max_flops','max_cells')) else 2.0)); det["synth"] = round(s, 3); det["area"] = sm["area"]
                if "period_ns" in t:
                    T = tools.sta(S["netlist"], ws.spec["top"], cwd=cwd, period_ns=t["period_ns"], clock_port=t["clock"])
                    w = T["metrics"]["wns"]
                    s_sta = 0.0 if w is None else (1.0 if w >= 0 else max(0.0, 1 + w / t["period_ns"]))
                    parts.append((s_sta, 3.0)); det["sta"] = round(s_sta, 3); det["wns"] = w
            if t["behaviour_preserving"]:
                gold = [str(ws.orig / f) for f in ws.spec["rtl"]]
                ports = tools._ports(gold, ws.spec["top"], cwd)
                split = [n for n, (d, wd) in ports.items() if d == "input" and wd <= 2 and n not in ("clk", "rst_n")] or None
                E = tools.equiv_smt(gold, rtl, ws.spec["top"], cwd=cwd, split=split, timeout=90)
                parts.append((1.0 if E["ok"] else 0.0, 3.0)); det["equiv"] = E["ok"]
            # public checks shipped with the task (smoke tests, replays, known-bad examples)
            for i, pc in enumerate(ws.spec.get("public_checks", [])):
                sp = self._public(ws, pc, i)
                parts.append((sp, 3.0)); det[f"pub{i}"] = round(sp, 3)
            # the agent's own tests / properties: never penalise looking. No tests = 0.4 (neutral);
            # tests = 0.4 + 0.6 * pass-rate; a test that failed earlier and passes now = bug found and fixed (+0.2).
            last, ever_failed = {}, set()
            for h in ws.history:
                if h["tool"].startswith(("sim:", "formal:", "cocotb:")):
                    last[h["tool"]] = h["score"]
                    if h["score"] < 1.0:
                        ever_failed.add(h["tool"])
            if not ws.spec.get("public_checks") or last:
                if last:
                    s_ev = 0.4 + 0.6 * sum(last.values()) / len(last)
                    if any(k in ever_failed and last[k] >= 1.0 for k in last):
                        s_ev = min(1.0, s_ev + 0.2)
                else:
                    s_ev = 0.4
                parts.append((s_ev, 1.5)); det["own_tests"] = round(s_ev, 3); det["n_own_tests"] = len(last)
            tw = sum(w for _, w in parts)
            res = (sum(s * w for s, w in parts) / tw, det)
        with self.lock:
            self.cache[key] = res
        return res

    def _public(self, ws, pc, i):
        """Score one public check. Shipped testbench files come from the ORIGINAL start/ (agent edits ignored);
        the agent's deliverables (stim, properties, cocotb tests) come from the workspace."""
        orig = ws.orig
        cwd = ws._scratch(f"prm_pub{i}")
        typ = pc["type"]
        if typ == "sim":
            r = tools.sim_iverilog(ws.rtl() + [str(orig / f) for f in pc["tb"]], pc.get("top", "tb"), cwd=cwd,
                                   params=pc.get("params"))
            return 1.0 if r["ok"] else min(0.8, r["metrics"]["passed"] / max(1, r["metrics"]["total"]))   # any failure caps at 0.8
        if typ == "gls":
            r = tools.gls(ws.rtl(), ws.spec["top"], [str(orig / f) for f in pc["tb"]], pc.get("top", "tb"), cwd=cwd,
                          params=pc.get("params"))
            return 1.0 if r["ok"] else min(0.8, r["metrics"]["passed"] / max(1, r["metrics"]["total"]))   # any failure caps at 0.8
        if typ == "cocotb":
            r = tools.sim_cocotb(ws.rtl(), ws.spec["top"], pc["module"], orig / pc["dir"], cwd=cwd)
            return 1.0 if r["ok"] else min(0.8, r["metrics"]["passed"] / max(1, r["metrics"]["total"]))   # any failure caps at 0.8
        if typ == "mutants_public":
            def run(rtl, sub):
                d = cwd / sub; d.mkdir(exist_ok=True)
                if pc["kind"] == "formal":
                    files = [ws.root / f for f in pc["files"]]
                    if not all(f.exists() for f in files):
                        return None
                    return tools.formal(rtl + [str(f) for f in files], pc["top"], cwd=d, mode=pc.get("mode", "prove"),
                                        depth=pc.get("depth", 20))["ok"]
                tdir = ws.root / pc["dir"]
                if not (tdir / f"{pc['module']}.py").exists():
                    return None
                return tools.sim_cocotb(rtl, ws.spec["top"], pc["module"], tdir, cwd=d)["ok"]
            gold = [str(orig / f) for f in ws.spec["rtl"]]
            g = run(gold, "gold")
            if not g:
                return 0.0
            bad = sorted(p for p in (orig / "known_bad").iterdir() if p.is_dir())
            killed = sum(1 for b in bad if run([str(b / Path(f).name) for f in ws.spec["rtl"]], b.name) is False)
            return 0.3 + 0.7 * killed / len(bad)      # proves/passes on golden = 0.3, each public kill adds
        raise ValueError(typ)

    def _value_batch_calibrated(self, parent_ws, parent_det, parent_info, children):
        """value = 0.55*exec + 0.30*calibrated judge + 0.15*progress (judge progress vote + exec delta)."""
        from concurrent.futures import ThreadPoolExecutor
        from agent.judge import describe, rtl_diff
        try:
            with ThreadPoolExecutor(max_workers=len(children)) as ex:
                execs = list(ex.map(lambda c: self.exec_score(c[0]), children))
            e_par = parent_det.get("exec") if parent_det and "exec" in parent_det else self.exec_score(parent_ws)[0]
            task = parent_ws.spec["prompt"]
            hist = "Task: " + task.strip().splitlines()[0] + "\n" + describe(parent_info, parent_det)
            raws = [describe(info, det) + "\nRTL CHANGE vs parent:\n" + rtl_diff(parent_ws, ws, ws.spec["rtl"])
                    for (ws, _, info), (_, det) in zip(children, execs)]
            with ThreadPoolExecutor(max_workers=len(children)) as ex:
                verdicts = list(ex.map(lambda r: self.cjudge.verdict(task, hist, r), raws))
        except Exception as e:
            return [(0.0, {"prm_error": repr(e)[:200]}) for _ in children]
        out = []
        for (e, det), v in zip(execs, verdicts):
            d = dict(det)
            delta = max(-1.0, min(1.0, 4 * (e - e_par)))
            progress = max(0.0, min(1.0, 0.5 + 0.25 * v.progress + 0.25 * delta))
            val = 0.55 * e + 0.30 * v.score + 0.15 * progress
            d.update(exec=round(e, 4), judge_cal=round(v.score, 3), judge_raw=round(v.raw, 3),
                     jprogress=round(v.progress, 2), delta=round(e - e_par, 3), evidence_ok=v.evidence_ok)
            out.append((val, d))
        return out

    # ------------------------------------------------------------------ judge part
    def judge(self, ws, messages):
        if self.client is None:
            return 0.5
        recent = []
        for m in messages[-10:]:
            if m["role"] == "assistant":
                for tc in m.get("tool_calls", []) or []:
                    a = tc["function"]["arguments"]
                    recent.append(f"ACTION {tc['function']['name']}({a[:300]})")
                if m.get("content"):
                    recent.append("SAYS: " + m["content"][:300])
            elif m["role"] == "tool":
                recent.append("RESULT: " + str(m["content"])[:600])
        rtl = "\n\n".join((ws.root / f).read_text(errors="ignore")[:3500] for f in ws.spec["rtl"] if (ws.root / f).exists())
        return self.client.p_yes(JUDGE_TMPL.format(prompt=ws.spec["prompt"], recent="\n".join(recent[-14:]), rtl=rtl))

    # ------------------------------------------------------------------ value
    def value(self, ws, messages):
        try:
            return self._value(ws, messages)
        except Exception as e:      # a malformed agent artefact must only sink its own branch
            return 0.0, {"prm_error": repr(e)[:200]}

    def _value(self, ws, messages):
        if self.mode == "oracle":
            g = grade(ws.task_dir, ws.root)
            return g["reward"], {"oracle_reward": g["reward"], "oracle_solved": g["solved"]}
        e, det = self.exec_score(ws)
        det = dict(det)                      # cached dict is shared across nodes; don't mutate it
        det["exec"] = round(e, 4)
        j = self.judge(ws, messages)
        det["judge"] = round(j, 3)
        return self.w_exec * e + (1 - self.w_exec) * j, det
