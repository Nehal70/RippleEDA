"""Calibrated Ripple judge for the tool-call search.

Drop-in for RippleActor.judge(history, raw) -> (score, evidence). The score is meant to be read as
"probability this branch ends in a design that passes signoff", so it sits on the same scale as the
rollout scores it is blended with in Node.blended_mean().

Design (paper idea -> what it became here, picked for Ripple-65B's strengths and weaknesses):
  * Ripple answers reliably only through a FORCED TOOL CALL with built-in reasoning off. Free text burns the
    whole token budget on hidden reasoning (measured: 56/64 empty answers). -> submit_judgement tool.
  * Reason before the verdict (GenRM / ThinkPRM)       -> `rationale` is the first field of the tool call.
  * Evidence grounding (this repo's rule)               -> `evidence` must be a quote that occurs in the tool log;
                                                           a missing/invented quote pulls the score to the prior.
  * Rubric (Prometheus 2)                               -> per-task acceptance checklist, generated once, cached.
  * Reward progress, not state (PAV, AgentPRM)          -> asks for progress vs the parent step as well as the
                                                           chance of finishing; both feed the calibrator.
  * Self-consistency (GenRM majority)                   -> `votes` samples; mean and spread are features.
  * Self-preference bias (Panickssery+ 2024)            -> the judge never sees the worker's rationale or prose,
                                                           only tool names, arguments and tool output.
  * Calibration (Platt scaling)                         -> logistic map from [raw score, progress, spread,
                                                           evidence ok] to probability, fitted by
                                                           scripts/calibrate_judge.py on labelled design states.
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "ricursiveai/ripple-65b-preview"
CALIBRATION_FILE = Path(__file__).with_name("judge_calibration.json")

RUBRIC_PROMPT = """You are a senior RTL design and verification signoff reviewer.
Write the acceptance checklist for this chip-design task: 4-10 concrete, checkable items covering
functional behaviour under all legal conditions, reset/X behaviour, interface/protocol rules, numeric
area/timing/flop targets, and lint/latch/loop cleanliness. For each item name the tool evidence that
proves it (lint, which simulation, formal, synthesis, timing, equivalence).
Only use requirements stated or clearly implied by the task.

TASK:
{task}"""

JUDGE_PROMPT = """You judge ONE step of a chip-design search. You are not the worker.
The worker already chose a tool call and the tool already ran.

TASK:
{task}

REFERENCE - SUCCESS CRITERIA (acceptance checklist for this task):
{rubric}

EARLIER STEPS ON THIS BRANCH (tool, arguments, short output):
{history}

THE STEP TO JUDGE - full tool output:
{raw}

Fill, in this order: evidence, rationale, p_success, progress.
  evidence:  a short quote copied EXACTLY from the tool output above (never from anything else).
  rationale: one or two sentences tying that evidence to the success criteria.
  p_success: probability that this branch ends in a design meeting EVERY success criterion. Anchors:
             0.0-0.2  the call failed, was refused, answered a different question, or shows failing checks
                      that this branch has not addressed;
             0.3-0.5  the call ran but does not show the task is closer to done (e.g. lint clean only);
             0.6-0.8  real progress: a verified failure was fixed or decisive evidence gathered, and the next
                      step is obvious;
             0.9-1.0  the output itself shows every success criterion met (all checks pass, targets met).
  progress:  compared with before this step: -1 worse (broke something / wasted the step), 0 no new
             information, +1 real progress.
Rules: a clean lint log is not success; a simulation that does not exercise a criterion proves nothing about
it; any failing check caps p_success at 0.5; do not reward length. Use the submit_judgement tool."""

JUDGE_TOOL = [{"type": "function", "function": {
    "name": "submit_judgement",
    "description": "Submit the judgement. Field order: evidence, rationale, p_success, progress.",
    "parameters": {"type": "object", "properties": {
        "evidence": {"type": "string", "description": "short quote copied exactly from the tool output"},
        "rationale": {"type": "string", "description": "one or two sentences tying the evidence to the criteria"},
        "p_success": {"type": "number", "description": "0..1, anchored bands"},
        "progress": {"type": "integer", "description": "-1, 0 or 1"}},
        "required": ["evidence", "rationale", "p_success", "progress"]}}}]

COMPARE_PROMPT = """Two branches of a chip-design search reached the states below. Which one is closer to a design
meeting EVERY success criterion? Judge only the tool evidence shown, not length or wording.

TASK:
{task}

REFERENCE - SUCCESS CRITERIA:
{rubric}

=== BRANCH A ===
{a}

=== BRANCH B ===
{b}

Fill, in this order: evidence (a short quote from the branch you prefer), rationale, winner ("A", "B" or "tie").
Use the submit_comparison tool."""

COMPARE_TOOL = [{"type": "function", "function": {
    "name": "submit_comparison",
    "description": "Submit the comparison. Field order: evidence, rationale, winner.",
    "parameters": {"type": "object", "properties": {
        "evidence": {"type": "string"}, "rationale": {"type": "string"},
        "winner": {"type": "string", "enum": ["A", "B", "tie"]}},
        "required": ["evidence", "rationale", "winner"]}}}]

RUBRIC_TOOL = [{"type": "function", "function": {
    "name": "submit_checklist",
    "description": "Submit the acceptance checklist.",
    "parameters": {"type": "object", "properties": {
        "items": {"type": "array", "items": {"type": "string"}}}, "required": ["items"]}}}]


def _load_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if key and not key.startswith("your_"):
        return key
    for f in (Path(__file__).resolve().parents[1] / ".env", Path.home() / "rippleeda" / ".env"):
        if f.exists():
            for line in f.read_text().splitlines():
                if line.strip().startswith("OPENROUTER_API_KEY="):
                    k = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if k and not k.startswith("your_"):
                        return k
    raise RuntimeError("OPENROUTER_API_KEY not set (environment or .env)")


def evidence_in_log(evidence: str, raw: str) -> bool:
    q = (evidence or "").strip()
    return len(q) >= 8 and q in (raw or "")


@dataclass
class Verdict:
    score: float                    # calibrated probability of branch success (what the search should use)
    raw: float                      # mean p_success over votes, before calibration
    progress: float                 # mean progress vote in [-1, 1]
    spread: float                   # std of p_success over votes
    evidence_ok: bool
    evidence: str
    rationale: str
    votes: list = field(default_factory=list)


class Calibrator:
    """Logistic (Platt-style) map over features -> probability. Identity-ish until fitted."""

    FEATURES = ("logit_raw", "progress", "spread", "evidence_ok")

    def __init__(self, weights=None, bias=0.0, meta=None):
        self.w = dict(weights or {"logit_raw": 1.0})
        self.b = bias
        self.meta = meta or {}

    @staticmethod
    def featurize(v: "Verdict | dict") -> dict:
        d = asdict(v) if isinstance(v, Verdict) else v
        r = min(1 - 1e-3, max(1e-3, float(d["raw"])))
        return {"logit_raw": math.log(r / (1 - r)), "progress": float(d["progress"]),
                "spread": float(d["spread"]), "evidence_ok": 1.0 if d["evidence_ok"] else 0.0}

    def predict(self, feats: dict) -> float:
        z = self.b + sum(self.w.get(k, 0.0) * feats.get(k, 0.0) for k in self.FEATURES)
        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))

    def fit(self, rows: list[dict], labels: list[float], l2=0.05, lr=0.2, iters=4000):
        """Plain logistic regression (soft labels allowed), no external deps."""
        w = {k: 0.0 for k in self.FEATURES}
        w["logit_raw"] = 1.0
        b = 0.0
        n = max(1, len(rows))
        for _ in range(iters):
            gw = {k: 0.0 for k in self.FEATURES}
            gb = 0.0
            for f, y in zip(rows, labels):
                p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, b + sum(w[k] * f[k] for k in self.FEATURES)))))
                e = p - y
                gb += e
                for k in self.FEATURES:
                    gw[k] += e * f[k]
            b -= lr * gb / n
            for k in self.FEATURES:
                w[k] -= lr * (gw[k] / n + l2 * w[k])
        self.w, self.b = w, b
        return self

    def save(self, path=CALIBRATION_FILE):
        Path(path).write_text(json.dumps({"weights": self.w, "bias": self.b, "meta": self.meta}, indent=2) + "\n")

    @classmethod
    def load(cls, path=CALIBRATION_FILE):
        p = Path(path)
        if not p.exists():
            return cls()
        d = json.loads(p.read_text())
        return cls(d.get("weights"), d.get("bias", 0.0), d.get("meta"))


class CalibratedJudge:
    def __init__(self, model: str | None = None, votes: int = 2, calibrator: Calibrator | None = None,
                 temperature: float = 0.6, timeout: int = 180):
        self.model = model or os.environ.get("PRM_JUDGE_MODEL", "") or DEFAULT_MODEL
        if self.model.startswith("your_"):
            self.model = DEFAULT_MODEL
        self.key = _load_key()
        self.votes = votes
        self.temperature = temperature
        self.timeout = timeout
        self.cal = calibrator if calibrator is not None else Calibrator.load()
        self._rubrics: dict[str, str] = {}
        self._lock = threading.Lock()
        self.calls = 0
        self.usd = 0.0

    # ------------------------------------------------------------------ transport
    def _chat(self, messages, tools, tool_name, temperature, seed=None):
        body = {"model": self.model, "messages": messages, "temperature": temperature, "max_tokens": 4000,
                "tools": tools, "tool_choice": {"type": "function", "function": {"name": tool_name}},
                "reasoning": {"enabled": False}}
        if seed is not None:
            body["seed"] = seed
        delay = 3.0
        for attempt in range(8):
            req = urllib.request.Request(API_URL, data=json.dumps(body).encode(), method="POST",
                                         headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    payload = json.loads(r.read().decode())
                with self._lock:
                    self.calls += 1
                    self.usd += float((payload.get("usage") or {}).get("cost") or 0.0)
                tcs = payload["choices"][0]["message"].get("tool_calls") or []
                return json.loads(tcs[0]["function"]["arguments"]) if tcs else {}
            except urllib.error.HTTPError as e:
                if e.code in (401, 402):            # bad key / out of credits: retrying cannot help
                    raise RuntimeError(f"OpenRouter HTTP {e.code}: {e.read().decode(errors='replace')[:200]}")
                if e.code == 429:                   # upstream rate limit: back off harder
                    delay = max(delay, 10.0)
            except Exception:
                pass
            time.sleep(delay + random.random() * 2)
            delay = min(delay * 2, 60)
        return {}

    # ------------------------------------------------------------------ rubric
    def rubric(self, task: str) -> str:
        key = task.strip()[:2000]
        with self._lock:
            if key in self._rubrics:
                return self._rubrics[key]
        out = self._chat([{"role": "user", "content": RUBRIC_PROMPT.format(task=task)}], RUBRIC_TOOL,
                         "submit_checklist", 0.0)
        items = out.get("items") or []
        txt = "\n".join(f"{i + 1}. {it}" for i, it in enumerate(items)) or \
            "1. Meets every requirement stated in the task.\n2. Lint/latch/loop clean.\n3. Meets the stated targets."
        with self._lock:
            self._rubrics[key] = txt
        return txt

    # ------------------------------------------------------------------ judging
    def verdict(self, task: str, history: str, raw: str) -> Verdict:
        prompt = JUDGE_PROMPT.format(task=task, rubric=self.rubric(task), history=_strip_rationale(history)[-6000:],
                                     raw=(raw or "(empty output)")[-12000:])

        def one(i):
            return self._chat([{"role": "user", "content": prompt}], JUDGE_TOOL, "submit_judgement",
                              self.temperature, seed=1000 + i)

        with ThreadPoolExecutor(max_workers=self.votes) as ex:
            outs = [o for o in ex.map(one, range(self.votes)) if o]
        if not outs:
            v = Verdict(0.5, 0.5, 0.0, 0.0, False, "", "judge returned nothing")
            v.score = self.cal.predict(Calibrator.featurize(v))
            return v
        ps = [min(1.0, max(0.0, float(o.get("p_success", 0.5) or 0.5))) for o in outs]
        pg = [max(-1.0, min(1.0, float(o.get("progress", 0) or 0))) for o in outs]
        ev = next((o.get("evidence", "") for o in outs if evidence_in_log(o.get("evidence", ""), raw)), "")
        mean = sum(ps) / len(ps)
        spread = (sum((p - mean) ** 2 for p in ps) / len(ps)) ** 0.5
        v = Verdict(score=0.0, raw=mean, progress=sum(pg) / len(pg), spread=spread, evidence_ok=bool(ev),
                    evidence=ev or (outs[0].get("evidence", "") or ""), rationale=str(outs[0].get("rationale", ""))[:600],
                    votes=[{"p": p, "progress": g} for p, g in zip(ps, pg)])
        v.score = self.cal.predict(Calibrator.featurize(v))
        return v

    def judge(self, history: str, raw: str, task: str | None = None) -> tuple[float, str]:
        """Same contract as RippleActor.judge: (score 0..1, evidence quote)."""
        task = task or _task_from_history(history)
        v = self.verdict(task, history, raw)
        return v.score, v.evidence

    def compare(self, task: str, state_a: str, state_b: str) -> str:
        """Pairwise mode for near-ties. Runs both orders; a branch wins only if it wins BOTH (else 'tie').
        state_a / state_b: what each branch's tools showed (history + latest output)."""
        rub = self.rubric(task)

        def ask(x, y, seed):
            out = self._chat([{"role": "user", "content": COMPARE_PROMPT.format(task=task, rubric=rub, a=x[-6000:], b=y[-6000:])}],
                             COMPARE_TOOL, "submit_comparison", 0.2, seed=seed)
            return str(out.get("winner", "tie")).strip().upper()

        with ThreadPoolExecutor(max_workers=2) as ex:
            fwd, rev = ex.map(lambda args: ask(*args), [(state_a, state_b, 11), (state_b, state_a, 12)])
        if fwd == "A" and rev == "B":
            return "A"
        if fwd == "B" and rev == "A":
            return "B"
        return "tie"

    def rerank_near_ties(self, task: str, siblings: list, describe, eps: float = 0.05) -> list:
        """Order siblings by score, then settle adjacent near-ties (|score gap| < eps) pairwise.
        siblings: objects with a mutable `.prior` (e.g. core.search_types.Node); describe(node) -> str.
        A pairwise win nudges the winner up by eps/2; ties leave scores alone. Never kills anything."""
        ordered = sorted(siblings, key=lambda n: -n.prior)
        for x, y in zip(ordered, ordered[1:]):
            if abs(x.prior - y.prior) < eps:
                w = self.compare(task, describe(x), describe(y))
                if w == "A":
                    x.prior = min(1.0, x.prior + eps / 2)
                elif w == "B":
                    y.prior = min(1.0, y.prior + eps / 2)
        return sorted(siblings, key=lambda n: -n.prior)


def _task_from_history(history: str) -> str:
    m = re.search(r"^Task:\s*(.*)$", history or "", flags=re.M)
    return m.group(1) if m else (history or "")[:1500]


def _strip_rationale(history: str) -> str:
    """Self-preference guard: drop any worker rationale/prose lines, keep tool calls and outputs."""
    keep = [l for l in (history or "").splitlines() if not re.match(r"\s*(rationale|reason|worker says)\s*:", l, re.I)]
    return "\n".join(keep)


class CalibratedRippleActor:
    """Wrap Nehal's RippleActor so LiveChipWorld uses the calibrated judge without other changes."""

    def __init__(self, actor, judge: CalibratedJudge, task: str):
        self._actor, self._judge, self._task = actor, judge, task

    def __getattr__(self, name):
        return getattr(self._actor, name)

    def judge(self, history: str, raw: str) -> tuple[float, str]:
        return self._judge.judge(history, raw, task=self._task)
