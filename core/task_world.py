"""TaskWorld: the core/ tree search (MCTS + verifier + calibrated judge) over a real task Workspace.

Same interface as LiveChipWorld (propose / act / rollout) but every tree node owns a forked
agent.workspace.Workspace, so edits on one branch never leak into another. Ripple is driven
through FORCED tool calls with reasoning disabled (the only reliable path for Ripple-65B).
"""
from __future__ import annotations

import json
import random
import threading
import time
import urllib.error
import urllib.request

from agent.agents import SYSTEM
from agent.workspace import TOOL_SCHEMAS, Workspace
from core.calibrated_judge import CalibratedJudge
from core.mcts_engine import ToolSearch
from core.search_types import Action, Claim, Node, ToolResult
from core.verifier import terminal_score

API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "ricursiveai/ripple-65b-preview"

WORKER_TOOLS = ["list_files", "read_file", "write_file", "run_lint", "run_synth", "run_sta", "run_sim", "submit"]
TOOL_TEXT = "\n".join(
    f"- {t['function']['name']}({', '.join(t['function']['parameters'].get('properties', {}).keys())}): {t['function']['description']}"
    for t in TOOL_SCHEMAS if t["function"]["name"] in WORKER_TOOLS
)

_ACTION_PROPS = {
    "tool": {"type": "string", "enum": WORKER_TOOLS},
    "rationale": {"type": "string", "description": "one short sentence"},
    "path": {"type": "string", "description": "read_file / write_file: workspace path"},
    "content": {"type": "string", "description": "write_file: the COMPLETE new file content"},
    "period_ns": {"type": "number", "description": "run_sta: clock period in ns"},
    "tb_files": {"type": "array", "items": {"type": "string"}, "description": "run_sim: testbench files"},
    "top": {"type": "string", "description": "run_sim: testbench top module"},
    "summary": {"type": "string", "description": "submit: what was done"},
}
PROPOSE_TOOL = [{"type": "function", "function": {
    "name": "propose_actions",
    "description": "Propose several DIFFERENT candidate next tool calls (different tools, or same tool with different args).",
    "parameters": {"type": "object", "properties": {
        "actions": {"type": "array", "items": {"type": "object", "properties": _ACTION_PROPS, "required": ["tool", "rationale"]}}},
        "required": ["actions"]}}}]
STEP_TOOL = [{"type": "function", "function": {
    "name": "next_action",
    "description": "The single next tool call to make.",
    "parameters": {"type": "object", "properties": _ACTION_PROPS, "required": ["tool", "rationale"]}}}]
CLAIM_TOOL = [{"type": "function", "function": {
    "name": "submit_claim",
    "description": "Report the numbers exactly as printed in the tool log. Leave a field out if the log does not show it.",
    "parameters": {"type": "object", "properties": {
        "error_count": {"type": "integer"}, "latch_count": {"type": "integer"}, "total_cells": {"type": "integer"},
        "tests_passed": {"type": "integer"}, "tests_total": {"type": "integer"},
        "conclusion": {"type": "string", "enum": ["pass", "fail", "hung", "edited", "counted", "unknown"]},
        "evidence": {"type": "string", "description": "short quote copied from the log"}},
        "required": ["conclusion", "evidence"]}}}]

CLAIM_KEYS = ("error_count", "latch_count", "total_cells", "tests_passed", "tests_total")


def _clip(s, n):
    s = str(s or "")
    return s if len(s) <= n else s[: n // 2] + "\n...[truncated]...\n" + s[-n // 2:]


def _args_from(item: dict) -> tuple[str, dict]:
    tool = str(item.get("tool", ""))
    a = {}
    if tool in ("read_file", "write_file") and item.get("path"):
        a["path"] = item["path"]
    if tool == "write_file":
        a["content"] = item.get("content") or ""
    if tool == "run_sta":
        a["period_ns"] = item.get("period_ns")
    if tool == "run_sim":
        a["tb_files"] = list(item.get("tb_files") or [])
        a["top"] = item.get("top") or "tb"
    if tool == "submit":
        a["summary"] = item.get("summary") or ""
    return tool, a


class ForcedRipple:
    """Forced-tool-call transport (reasoning off). Counts calls and USD."""

    def __init__(self, key: str, model: str | None = None, timeout: int = 240):
        self.key, self.model, self.timeout = key, model or DEFAULT_MODEL, timeout
        self.calls, self.usd = 0, 0.0
        self._lock = threading.Lock()

    def chat(self, messages, tools, tool_name, temperature, seed=None, max_tokens=12000) -> dict:
        body = {"model": self.model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens,
                "tools": tools, "tool_choice": {"type": "function", "function": {"name": tool_name}},
                "reasoning": {"enabled": False}, "usage": {"include": True}}
        if seed is not None:
            body["seed"] = seed
        delay = 2.0
        self.last_error = ""
        for _ in range(40):  # upstream 429s can last minutes under load; keep polling rather than drop the node
            req = urllib.request.Request(API_URL, data=json.dumps(body).encode(), method="POST",
                                         headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    payload = json.loads(r.read().decode())
                with self._lock:
                    self.calls += 1
                    self.usd += float((payload.get("usage") or {}).get("cost") or 0.0)
                tcs = payload["choices"][0]["message"].get("tool_calls") or []
                if tcs:
                    return json.loads(tcs[0]["function"]["arguments"])
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:300]
                self.last_error = f"HTTP {e.code}: {detail}"
                if e.code in (401, 402):
                    raise RuntimeError(f"OpenRouter HTTP {e.code}")
                if e.code == 400:
                    raise RuntimeError(f"OpenRouter HTTP 400: {detail}")
                if e.code == 429:  # upstream shared pool: Retry-After is ~2 s, so poll gently
                    time.sleep(5.0 + random.random() * 6)
                    continue
            except Exception as e:
                self.last_error = repr(e)[:300]
            time.sleep(delay + random.random() * 2)
            delay = min(delay * 2, 30)
        return {}


class TaskWorld(ToolSearch):
    def __init__(self, task_dir, ripple: ForcedRipple, judge: CalibratedJudge, rollout_steps: int = 4,
                 log=None):
        self.ripple, self.judge, self.rollout_steps = ripple, judge, rollout_steps
        self.log = log or (lambda s: None)
        self.root_ws = Workspace(task_dir)
        self.spec = self.root_ws.spec
        self.all_ws = [self.root_ws]
        self.task = self.spec["prompt"].strip()
        self.period = next((c["period_ns"] for c in self.spec["checks"] if c["type"] == "sta"), None)
        self.public = [c for c in self.spec.get("public_checks") or [] if c["type"] == "sim"]
        self.constraints = self._constraints()
        self.baseline = self._measure_baseline()
        self.constraints["baseline"] = self.baseline
        # If the start design already misses timing, a hard delay cap would kill every branch that
        # merely measures STA; keep delay as a scored PPA term (ratio vs baseline) instead.
        cp = self.baseline.get("critical_path_ns")
        if cp is not None and self.constraints.get("max_delay") and cp > self.constraints["max_delay"]:
            self.log(f"start design misses timing ({cp} > {self.constraints['max_delay']}); dropping max_delay hard cap")
            self.constraints.pop("max_delay")
        self.system = self._system()

    # ------------------------------------------------------------------ setup
    def _constraints(self) -> dict:
        c: dict = {}
        for chk in self.spec["checks"]:
            if chk["type"] == "synth":
                if chk.get("no_latches", True):
                    c["forbid_latches"] = True
                if "max_cells" in chk:
                    c["max_cells"] = chk["max_cells"]
        if self.period:
            c["max_delay"] = float(self.period)
        return c

    def _measure_baseline(self) -> dict:
        ws = self.root_ws.fork()
        truth: dict = {}
        try:
            for tool, args in self._measure_plan():
                out = ws.call(tool, args)
                truth.update(self._truth(ws, tool, args, out))
        finally:
            ws.cleanup()
        self.log(f"baseline truth: {truth}")
        return truth

    def _measure_plan(self):
        plan = [("run_lint", {}), ("run_synth", {})]
        if self.period:
            plan.append(("run_sta", {"period_ns": float(self.period)}))
        for pc in self.public:
            plan.append(("run_sim", {"tb_files": list(pc["tb"]), "top": pc.get("top", "tb")}))
        return plan

    def _system(self) -> str:
        hint = ""
        if self.public:
            pc = self.public[0]
            hint = (f"\nThe block's public smoke test is already in the workspace: run it with "
                    f"run_sim(tb_files={list(pc['tb'])!r}, top={pc.get('top', 'tb')!r}). It must pass before you submit.")
        if self.period:
            hint += f"\nTiming target: run_sta(period_ns={self.period})."
        return (SYSTEM + "\n\nAvailable tools (choose them through the function you are forced to call):\n"
                + TOOL_TEXT + hint
                + "\nRules: write_file must contain the COMPLETE file. Never repeat a call whose output you already have. "
                  "Fix bugs one at a time and re-run the smoke test after each edit. Call submit only once the smoke test "
                  "passes, lint is clean, synthesis has no latches and timing is met.")

    # ------------------------------------------------------------------ helpers
    def ws_of(self, node: Node) -> Workspace:
        if node.result is not None and getattr(node.result, "ws", None) is not None:
            return node.result.ws
        return self.root_ws

    def _history(self, node: Node, last_full: int = 3000, older: int = 1200) -> str:
        lines = [f"Task: {self.task}", f"Workspace files: {self.spec['rtl']}"]
        steps = [s for s in node.path() if s.action is not None]
        for i, step in enumerate(steps):
            args = {k: (v if len(str(v)) < 300 else str(v)[:300] + "...") for k, v in step.action.args.items()}
            raw = _clip(step.result.raw if step.result else "", last_full if i == len(steps) - 1 else older)
            lines.append(f"Called {step.action.tool} {json.dumps(args)}\nOutput: {raw}")
        return "\n".join(lines)

    def _truth(self, ws: Workspace, tool: str, args: dict, out: str) -> dict:
        """Map the Workspace's recorded metrics for this call onto verifier keys."""
        t: dict = {}
        rec = ws.history[-1] if ws.history else None
        m = (rec or {}).get("metrics") or {}
        if tool == "run_lint" and rec and rec["tool"] == "lint":
            t["error_count"] = int(m.get("errors", 0))
        elif tool in ("run_synth", "run_sta") and rec and rec["tool"] in ("synth", "sta"):
            if rec["tool"] == "synth" and not m:
                t["error_count"] = 1
            else:
                if "cells" in m:
                    t["total_cells"] = int(m["cells"])
                if "latches" in m:
                    t["latch_count"] = int(m["latches"])
                if "area" in m:
                    t["cell_area"] = float(m["area"])
                if rec["tool"] == "sta":
                    w = m.get("wns")
                    if w is not None:
                        t["critical_path_ns"] = round(float(args.get("period_ns") or self.period or 0) - float(w), 4)
                    t["wns"] = w
        elif tool == "run_sim" and rec and rec["tool"].startswith("sim:"):
            if m.get("compile_error"):
                t["error_count"] = 1
            else:
                t["tests_passed"] = int(m.get("passed", 0))
                t["tests_total"] = int(m.get("total", 0))
                t["sim_hung"] = bool(m.get("timeout"))
        elif tool == "write_file":
            t["edited"] = True
        elif tool == "submit":
            t["submitted"] = True
        return t

    def _exec(self, ws: Workspace, action: Action) -> ToolResult:
        n0 = len(ws.history)
        if action.tool not in WORKER_TOOLS:
            return ToolResult(ok=False, raw=f"unknown tool {action.tool}", truth={})
        out = str(ws.call(action.tool, action.args))
        ran = len(ws.history) > n0
        truth = self._truth(ws, action.tool, action.args, out) if (ran or action.tool in ("write_file", "submit", "read_file", "list_files")) else {}
        ok = not (out.startswith("bad arguments") or " failed: " in out[:80] or out.startswith("synthesis FAILED")
                  or out.startswith("unknown tool"))
        if action.tool in ("run_lint", "run_synth", "run_sta", "run_sim") and not ran:
            ok = False
        if truth.get("error_count", 0) > 0 and action.tool in ("run_lint", "run_sim"):
            ok = False
        if action.tool == "read_file" and out.startswith("read_file failed"):
            ok = False
        return ToolResult(ok=ok, raw=_clip(out, 3000), truth=truth)

    def _claim(self, raw: str, truth: dict) -> Claim:
        keys = [k for k in CLAIM_KEYS if k in truth]
        if not keys:
            return Claim(fields={}, conclusion="unknown", evidence="")
        prompt = (f"Read this tool log and fill ONLY these fields from numbers printed in it: {', '.join(keys)}.\n"
                  f"Do not invent numbers.\n\nLOG:\n{raw[-6000:]}")
        out = self.ripple.chat([{"role": "user", "content": prompt}], CLAIM_TOOL, "submit_claim", 0.0)
        fields = {}
        for k in keys:
            if k in out and out[k] is not None:
                try:
                    fields[k] = int(out[k])
                except (TypeError, ValueError):
                    pass
        return Claim(fields=fields, conclusion=str(out.get("conclusion", "unknown")), evidence=str(out.get("evidence", "")))

    def _ask_actions(self, node: Node, k: int, temperature: float, seed=None) -> list[Action]:
        msgs = [{"role": "system", "content": self.system},
                {"role": "user", "content": self._history(node) + f"\n\nPropose {k} different next actions."}]
        out = self.ripple.chat(msgs, PROPOSE_TOOL, "propose_actions", temperature, seed=seed)
        if not out:
            self.log(f"propose call returned nothing: {self.ripple.last_error}")
        acts = []
        for item in (out.get("actions") or [])[: k * 2]:
            if not isinstance(item, dict):
                continue
            tool, args = _args_from(item)
            if tool in WORKER_TOOLS:
                acts.append(Action(tool=tool, args=args, rationale=str(item.get("rationale", ""))[:300]))
        return acts

    def _ask_one(self, node: Node, temperature: float = 0.3, seed=None) -> Action | None:
        msgs = [{"role": "system", "content": self.system},
                {"role": "user", "content": self._history(node) + "\n\nChoose the single next action."}]
        out = self.ripple.chat(msgs, STEP_TOOL, "next_action", temperature, seed=seed)
        tool, args = _args_from(out) if isinstance(out, dict) else ("", {})
        if tool not in WORKER_TOOLS:
            return None
        return Action(tool=tool, args=args, rationale=str(out.get("rationale", ""))[:300])

    # ------------------------------------------------------------------ ToolSearch interface
    def propose(self, node: Node, k: int) -> list[Action]:
        acts, seen = [], set()
        for a in self._ask_actions(node, k, 0.8, seed=random.randrange(1 << 30)):
            if a.key() in seen:
                continue
            seen.add(a.key())
            acts.append(a)
        if len(acts) < k:  # one more sample to fill in distinct ideas
            for a in self._ask_actions(node, k, 1.0, seed=random.randrange(1 << 30)):
                if a.key() not in seen and len(acts) < k:
                    seen.add(a.key())
                    acts.append(a)
        self.log(f"propose@{node.node_id}: {[a.tool for a in acts]}")
        return acts[:k]

    def act(self, node: Node, action: Action):
        ws = self.ws_of(node).fork()
        self.all_ws.append(ws)
        result = self._exec(ws, action)
        result.ws = ws  # type: ignore[attr-defined]
        claim = self._claim(result.raw, result.truth) if result.ok else Claim(fields={}, conclusion="fail", evidence="")
        try:
            v = self.judge.verdict(self.task, self._history(node), result.raw)
            score = float(v.score)
        except Exception as e:  # judge must never crash the search
            self.log(f"judge error: {e!r}")
            score = 0.5
        self.log(f"act@{node.node_id}: {action.tool} {str(action.args)[:80]} ok={result.ok} truth={result.truth} judge={score:.2f}")
        return result, claim, max(0.0, min(1.0, score))

    step = act

    def branch_truth(self, node: Node) -> dict:
        t: dict = {}
        for s in node.path():
            if s.result is not None:
                t.update(s.result.truth)
        return t

    def rollout(self, node: Node) -> tuple[float, bool, str]:
        latest = self.branch_truth(node)
        story, calls = [], 0
        if latest.get("submitted"):
            steps = node.depth
        else:
            ws = self.ws_of(node).fork()
            self.all_ws.append(ws)
            cur = node
            for _ in range(self.rollout_steps):
                action = self._ask_one(cur, seed=random.randrange(1 << 30))
                calls += 1
                if action is None:
                    story.append("no action")
                    break
                result = self._exec(ws, action)
                latest = {**latest, **result.truth}
                story.append(f"{action.tool} -> {'ok' if result.ok else 'fail'}")
                cur = Node(node_id="roll", action=action, parent=cur, result=result, depth=cur.depth + 1)
                if not result.ok or action.tool == "submit":
                    break
            node.rollout_ws = ws  # type: ignore[attr-defined]  kept so the runner can grade it if it is the best
            steps = node.depth + calls
        score, passed = terminal_score(latest, self.constraints, baseline=self.baseline,
                                       weights=self.constraints.get("weights"), steps=steps)
        if not passed and latest.get("error_count", 1) == 0 and score < 0.35:
            score = max(score, 0.35)
        self.log(f"rollout@{node.node_id}: {'; '.join(story) or 'none'} truth={latest} score={score:.2f} passed={passed}")
        node.rollout_truth = latest  # type: ignore[attr-defined]
        return score, passed, "; ".join(story) or "no further calls"

    def cleanup(self):
        for ws in self.all_ws:
            ws.cleanup()
