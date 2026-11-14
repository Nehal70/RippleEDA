"""
Judge v2 — listwise, reasoning, progress-aware LLM jury for RTL/DV agent steps.

Design (paper -> mechanism):
  * Self-preference bias (Panickssery+ 2024)        -> judges are NOT the policy model (no Ripple in the jury)
  * Juries > single judge (PoLL, Verga+ 2024)         -> several open-weight models from different families, averaged
  * Rubric-based judging (Prometheus 2)               -> per-task acceptance checklist generated once from the prompt
  * Reason before verdict (GenRM / ThinkPRM / GenPRM) -> judge thinks, then emits JSON scores
  * Reward progress (PAV, Setlur+ 2024; AgentPRM)     -> scores both PROMISE (will this branch finish?) and PROGRESS vs parent
  * Position bias (Zheng+ 2023, MT-Bench)             -> siblings ranked together, in forward and reversed order, averaged
  * RTL-specific evidence (VerilogCoder, MAGE)        -> each candidate shows its RTL diff vs parent, failing-check lines,
                                                         and the public check breakdown
"""
from __future__ import annotations

import difflib
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor

DEFAULT_JURY = ["deepseek/deepseek-v4.1-flash-1017", "qwen/qwen3.8-27b"]

RUBRIC_PROMPT = """You are a senior RTL design/verification signoff reviewer.
Write the acceptance checklist for the task below: 5-10 numbered, concrete, checkable criteria
(functional behaviour under all legal conditions, reset/X behaviour, interface/protocol rules,
numeric PPA targets, lint/latch/loop cleanliness). For each, name the tool evidence that proves it
(lint / simulation with which testbench / formal / synthesis / STA / equivalence).
Only use requirements stated or clearly implied by the task. No preamble.

TASK:
{prompt}

PUBLIC CHECKS SHIPPED WITH THE TASK:
{public}"""

RANK_PROMPT = """You are judging the next step of an RTL/DV engineering agent. Be a strict signoff reviewer.

TASK:
{prompt}

ACCEPTANCE CHECKLIST:
{rubric}

PARENT STATE (before the step):
{parent}

CANDIDATE NEXT STEPS (independent alternatives from the same parent):
{cands}

For EACH candidate estimate:
  promise  = probability (0-1) that continuing from this candidate leads to a submission meeting EVERY checklist item
  progress = how much this step moved toward that compared with the parent: -1 (made it worse), 0 (no real progress,
             e.g. re-running a tool with no new information), +1 (clear progress, e.g. fixed a verified failure,
             gathered decisive evidence, closed a target)
Rules: prefer verified evidence over claims; a fix that silences a check by weakening behaviour is worse, not better;
lint-clean is necessary, not sufficient; penalise edits not followed by re-verification; do not reward verbosity.
Think briefly first. Then output ONE final JSON object on the last line, exactly:
{{"scores": {{"<label>": {{"promise": <0-1>, "progress": <-1|0|1>}}, ...}}}}"""


def _clip(s, n):
    s = str(s)
    return s if len(s) <= n else s[: n - 40] + f"\n...[{len(s) - n + 40} chars truncated]"


def rtl_diff(ws_a, ws_b, files, n=2200):
    out = []
    for f in files:
        a = (ws_a.root / f).read_text(errors="ignore").splitlines() if (ws_a.root / f).exists() else []
        b = (ws_b.root / f).read_text(errors="ignore").splitlines() if (ws_b.root / f).exists() else []
        out += list(difflib.unified_diff(a, b, f"parent/{f}", f"cand/{f}", n=1, lineterm=""))
    for p in ["recipe.json"]:
        if (ws_b.root / p).exists() and (not (ws_a.root / p).exists() or (ws_a.root / p).read_text() != (ws_b.root / p).read_text()):
            out.append(f"recipe.json -> {(ws_b.root / p).read_text()[:300]}")
    return _clip("\n".join(out) or "(no RTL change)", n)


def describe(info, exec_det):
    acts = []
    for a in (info or {}).get("actions", []):
        args = {k: v for k, v in a.get("args", {}).items() if k != "content"}
        res = str(a.get("result", ""))
        errs = [l for l in res.splitlines() if re.search(r"ERROR|FAIL|failed|Warning|WNS|passed=|status=|area=", l)][:6]
        acts.append(f"- {a['tool']}({json.dumps(args)[:160]}) -> " + (" | ".join(errs) if errs else res[:200].replace("\n", " ")))
    ev = {k: v for k, v in (exec_det or {}).items() if k not in ("judge", "exec", "prm_error")}
    return ("ACTIONS:\n" + ("\n".join(acts) or "- (none)") + f"\nCURRENT CHECK RESULTS (public): {json.dumps(ev)}")


def _norm_key(k):
    """'A', 'a', 'Candidate A', 'candidate_a', '(A)' -> 'A'"""
    m = re.search(r"([A-Za-z])\W*$", str(k).strip())
    return m.group(1).upper() if m else str(k)


def _parse(txt, labels):
    """Take the last JSON object containing "scores"; tolerate key spellings and fenced blocks."""
    blobs = re.findall(r"\{[^{}]*\"scores\"\s*:\s*\{.*?\}\s*\}\s*\}", txt, flags=re.S) or \
        re.findall(r"\{.*\}", txt, flags=re.S)
    for blob in reversed(blobs):
        try:
            d = json.loads(blob)
            d = d.get("scores", d)
            d = {_norm_key(k): v for k, v in d.items()}
            out = {}
            for L in labels:
                v = d.get(L)
                if not isinstance(v, dict):
                    continue
                pr = float(v.get("promise", 0.5)); pg = float(v.get("progress", 0))
                out[L] = (min(1.0, max(0.0, pr)), min(1.0, max(-1.0, pg)))
            if out:
                return out
        except Exception:
            continue
    return {}


PROBE_PROMPT = """You are a verification engineer trying to FALSIFY a candidate RTL fix. Do not trust it.

TASK:
{prompt}

ACCEPTANCE CHECKLIST:
{rubric}

EVIDENCE ALREADY GATHERED (public checks): {evidence}

CANDIDATE RTL:
{rtl}

Write ONE small, self-contained SystemVerilog testbench, module name `probe_tb`, that instantiates the design
(top module `{top}`, use its exact port names) and checks the checklist requirement you think is MOST LIKELY still
violated and not already covered by the evidence. Requirements: Icarus Verilog -g2012 compatible; drive a clock if
needed; print `ERROR: <what>` on each mismatch and `RESULT <passed> <total>` exactly once at the end; call $finish
within 5000 cycles. Output only one ```systemverilog code block."""


SCORE_TOOL = [{"type": "function", "function": {
    "name": "submit_scores",
    "description": "Submit your judgement. Write the rationale FIRST (your reasoning), then the scores.",
    "parameters": {"type": "object", "properties": {
        "rationale": {"type": "string", "description": "brief reasoning per candidate against the checklist"},
        "scores": {"type": "object", "description": "label -> {promise: 0..1, progress: -1|0|1}",
                   "additionalProperties": {"type": "object", "properties": {
                       "promise": {"type": "number"}, "progress": {"type": "number"}}}}},
        "required": ["rationale", "scores"]}}}]

RUBRIC_TOOL = [{"type": "function", "function": {
    "name": "submit_checklist",
    "description": "Submit the acceptance checklist.",
    "parameters": {"type": "object", "properties": {
        "items": {"type": "array", "items": {"type": "string"},
                  "description": "5-10 concrete checkable criteria, each naming the tool evidence that proves it"}},
        "required": ["items"]}}}]

PROBE_TOOL = [{"type": "function", "function": {
    "name": "submit_probe",
    "description": "Submit the falsifying probe testbench.",
    "parameters": {"type": "object", "properties": {
        "target_requirement": {"type": "string", "description": "which checklist item this probe attacks and why"},
        "code": {"type": "string", "description": "complete SystemVerilog source of module probe_tb"}},
        "required": ["target_requirement", "code"]}}}]

# Output channel: a FORCED TOOL CALL with built-in reasoning disabled. Measured on Ripple-65B: free-text answers
# (any reasoning setting) burned the whole token budget and returned nothing; forced tool calls answered in 3-15 s.
# Reasoning still happens, inside the tool's `rationale` / `target_requirement` field, before the scores.
_TOOL_EB = {"reasoning": {"enabled": False}}


class Jury:
    def __init__(self, make_client, models=None, orders=2):
        self.models = models or DEFAULT_JURY
        self.clients = {m: make_client(m) for m in self.models}
        self.orders = orders
        self._rubrics, self._lock = {}, threading.Lock()
        self.stats = {}   # model -> [calls, parse_failures]

    # ------------------------------------------------------------ rubric (cached per task)
    def rubric(self, spec):
        with self._lock:
            if spec["id"] in self._rubrics:
                return self._rubrics[spec["id"]]
        pub = "\n".join(json.dumps(p) for p in spec.get("public_checks", [])) or "(none)"
        c = self.clients[self.models[0]]
        txt = ""
        try:
            r = c._create(messages=[{"role": "user", "content": RUBRIC_PROMPT.format(prompt=spec["prompt"], public=pub)
                                     + "\nSubmit it with the submit_checklist tool."}],
                          temperature=0.0, max_tokens=3000, tools=RUBRIC_TOOL,
                          tool_choice={"type": "function", "function": {"name": "submit_checklist"}}, extra_body=_TOOL_EB)
            msg = r.choices[0].message
            items = json.loads(msg.tool_calls[0].function.arguments).get("items", []) if msg.tool_calls else []
            txt = "\n".join(f"{i + 1}. {it}" for i, it in enumerate(items))
        except Exception:
            pass
        txt = txt or "1. Meets every requirement stated in the task.\n2. Lint/latch/loop clean.\n3. Meets the stated PPA targets."
        with self._lock:
            self._rubrics[spec["id"]] = txt
        return txt

    # ------------------------------------------------------------ judge-by-doing probe
    def probe_code(self, ws, evidence):
        """Ask the judge for a falsifying testbench for `ws`. Returns code or None."""
        spec = ws.spec
        rtl = "\n\n".join(_clip((ws.root / f).read_text(errors="ignore"), 6000) for f in spec["rtl"] if (ws.root / f).exists())
        try:
            r = self.clients[self.models[0]]._create(messages=[{"role": "user", "content": PROBE_PROMPT.format(
                prompt=spec["prompt"], rubric=self.rubric(spec), evidence=json.dumps(evidence)[:800], rtl=rtl, top=spec["top"]
            ).replace("Output only one ```systemverilog code block.", "Submit it with the submit_probe tool.")}],
                temperature=0.4, max_tokens=8000, tools=PROBE_TOOL,
                tool_choice={"type": "function", "function": {"name": "submit_probe"}}, extra_body=_TOOL_EB)
            msg = r.choices[0].message
            code = json.loads(msg.tool_calls[0].function.arguments).get("code", "") if msg.tool_calls else ""
        except Exception:
            return None
        code = re.sub(r"^```\w*\s*|```\s*$", "", code.strip())
        return code if "probe_tb" in code else None

    @staticmethod
    def run_probe(ws, code, tag="probe"):
        """Run a probe testbench on a workspace. Returns 'pass' | 'fail' | 'invalid', detail."""
        from eda import tools
        d = ws.root / f"_run_{tag}"
        d.mkdir(exist_ok=True)
        (d / "probe_tb.sv").write_text(code)
        res = tools.sim_iverilog(ws.rtl() + [str(d / "probe_tb.sv")], "probe_tb", cwd=d, timeout=40)
        met = res["metrics"]
        if met.get("compile_error") or met.get("timeout"):
            return "invalid", (res.get("log") or "")[-160:]
        if res["ok"]:
            return "pass", f"{met['passed']}/{met['total']}"
        return "fail", "; ".join(res.get("first_errors", [])[:2])[:200] or f"{met['passed']}/{met['total']}"

    def probe(self, ws, evidence):
        """The judge writes a falsifying directed test; the real simulator runs it on the candidate.
        Returns ("fail"|"pass"|"invalid", detail). Invalid probes (do not compile / no RESULT) are ignored."""
        from eda import tools
        spec = ws.spec
        rtl = "\n\n".join(_clip((ws.root / f).read_text(errors="ignore"), 6000) for f in spec["rtl"] if (ws.root / f).exists())
        c = self.clients[self.models[0]]
        try:
            r = c._create(messages=[{"role": "user", "content": PROBE_PROMPT.format(
                prompt=spec["prompt"], rubric=self.rubric(spec), evidence=json.dumps(evidence)[:800], rtl=rtl, top=spec["top"]
            ).replace("Output only one ```systemverilog code block.", "Submit it with the submit_probe tool.")}],
                temperature=0.4, max_tokens=8000, tools=PROBE_TOOL,
                tool_choice={"type": "function", "function": {"name": "submit_probe"}}, extra_body=_TOOL_EB)
            msg = r.choices[0].message
            code = json.loads(msg.tool_calls[0].function.arguments).get("code", "") if msg.tool_calls else ""
        except Exception as e:
            return "invalid", repr(e)[:120]
        code = re.sub(r"^```\w*\s*|```\s*$", "", code.strip())
        if "probe_tb" not in code:
            return "invalid", "no testbench"
        d = ws.root / "_run_probe"
        d.mkdir(exist_ok=True)
        (d / "probe_tb.sv").write_text(code)
        res = tools.sim_iverilog(ws.rtl() + [str(d / "probe_tb.sv")], "probe_tb", cwd=d, timeout=40)
        met = res["metrics"]
        if met.get("compile_error") or met.get("timeout"):
            return "invalid", (res.get("log") or "")[-200:]
        if res["ok"]:
            return "pass", f"{met['passed']}/{met['total']}"
        return "fail", "; ".join(res.get("first_errors", [])[:2])[:240] or f"{met['passed']}/{met['total']}"

    # ------------------------------------------------------------ listwise scoring
    def score(self, spec, parent_desc, cand_descs):
        """cand_descs: list of strings. Returns list of (promise, progress) averaged over jury x orders."""
        n = len(cand_descs)
        labels = [chr(ord("A") + i) for i in range(n)]
        rub = self.rubric(spec)
        perms = [list(range(n))] + ([list(reversed(range(n)))] if self.orders > 1 and n > 1 else [])
        jobs = [(m, p) for m in self.models for p in perms]

        def run(job):
            m, perm = job
            # perm[j] = which candidate is shown under label j
            body = "\n\n".join(f"=== Candidate {labels[j]} ===\n{cand_descs[perm[j]]}" for j in range(n))
            prompt = RANK_PROMPT.format(prompt=spec["prompt"], rubric=rub, parent=parent_desc, cands=body).replace(
                "Think briefly first. Then output ONE final JSON object on the last line, exactly:",
                "Reason briefly in `rationale`, then give scores via the submit_scores tool, keyed by label:")
            try:
                r = self.clients[m]._create(messages=[{"role": "user", "content": prompt}], temperature=0.3,
                                            max_tokens=4000, tools=SCORE_TOOL,
                                            tool_choice={"type": "function", "function": {"name": "submit_scores"}},
                                            extra_body=_TOOL_EB)
                msg = r.choices[0].message
                txt = msg.tool_calls[0].function.arguments if msg.tool_calls else (msg.content or "")
                got = _parse(txt, labels)
                with self._lock:
                    self.stats[m] = self.stats.get(m, [0, 0]); self.stats[m][0] += 1; self.stats[m][1] += (len(got) < n)
            except Exception:
                got = {}
            return {perm[j]: got[labels[j]] for j in range(n) if labels[j] in got}

        with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
            results = list(ex.map(run, jobs))
        out = []
        for i in range(n):
            vals = [r[i] for r in results if i in r]
            if not vals:
                out.append((0.5, 0.0))
            else:
                out.append((sum(v[0] for v in vals) / len(vals), sum(v[1] for v in vals) / len(vals)))
        return out
