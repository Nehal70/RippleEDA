"""
Ripple-65B client (OpenRouter, OpenAI-compatible) + a scripted mock for dry runs.

    export OPENROUTER_API_KEY=...        # never hard-code it
    RIPPLE_MODEL defaults to ricursiveai/ripple-65b-preview
"""
from __future__ import annotations

import json
import math
import re
import os
import random
import threading
import time

MODEL = os.environ.get("RIPPLE_MODEL", "ricursiveai/ripple-65b-preview")
PRICE_IN, PRICE_OUT = 0.10e-6, 0.30e-6  # $/token on OpenRouter (2026-11)


class Usage:
    def __init__(self):
        self.lock = threading.Lock()
        self.calls = self.prompt = self.completion = 0

    def add(self, u):
        with self.lock:
            self.calls += 1
            if u:
                self.prompt += getattr(u, "prompt_tokens", 0) or 0
                self.completion += getattr(u, "completion_tokens", 0) or 0

    def cost(self):
        return self.prompt * PRICE_IN + self.completion * PRICE_OUT

    def as_dict(self):
        return {"calls": self.calls, "prompt_tokens": self.prompt, "completion_tokens": self.completion,
                "usd": round(self.cost(), 4)}


def _dotenv_key():
    """Read OPENROUTER_API_KEY from ~/rippleeda/.env (never printed or logged)."""
    from pathlib import Path
    f = Path(__file__).resolve().parent.parent / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            if line.strip().startswith("OPENROUTER_API_KEY="):
                k = line.split("=", 1)[1].strip().strip('"').strip("'")
                if k and not k.startswith("your_"):
                    return k
    return None


class RippleClient:
    def __init__(self, model=MODEL, usage=None):
        from openai import OpenAI
        key = os.environ.get("OPENROUTER_API_KEY") or _dotenv_key()
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY is not set")
        self.c = OpenAI(api_key=key, base_url="https://openrouter.ai/api/v1", timeout=300, max_retries=0)
        self.model = model
        self.usage = usage or Usage()

    def _create(self, **kw):
        delay = 2.0
        for attempt in range(6):
            try:
                r = self.c.chat.completions.create(model=self.model, **kw)
                self.usage.add(getattr(r, "usage", None))
                if not r.choices:
                    raise RuntimeError(f"empty choices: {r}")
                return r
            except Exception as e:  # rate limits / provider hiccups
                if attempt == 5 or "Error code: 402" in str(e) or "Error code: 401" in str(e):
                    raise                     # out of credits / bad key: retrying won't help
                time.sleep(delay + random.random())
                delay = min(delay * 2, 60)

    def chat(self, messages, tools=None, temperature=0.7, max_tokens=8192, seed=None):
        """Returns (assistant_message_dict, reasoning_text)."""
        kw = dict(messages=messages, temperature=temperature, max_tokens=max_tokens,
                  extra_body={"reasoning": {"enabled": True}, "include_reasoning": True})
        if tools:
            kw["tools"], kw["tool_choice"] = tools, "auto"
        if seed is not None:
            kw["seed"] = seed
        r = self._create(**kw)
        m = r.choices[0].message
        msg = {"role": "assistant", "content": m.content or ""}
        if m.tool_calls:
            msg["tool_calls"] = [{"id": tc.id, "type": "function",
                                  "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}}
                                 for tc in m.tool_calls]
        reasoning = getattr(m, "reasoning", None) or (getattr(m, "model_extra", None) or {}).get("reasoning") or ""
        return msg, reasoning

    def p_yes(self, prompt, votes=3):
        """Generative-PRM score (GenRM / ThinkPRM style): the judge reasons briefly, ends with
        'ANSWER: Yes|No'; score = fraction of Yes over `votes` samples. Unparseable votes are dropped."""
        ask = prompt + "\n\nThink step by step (briefly), then finish with a final line exactly 'ANSWER: Yes' or 'ANSWER: No'."
        def one(i):
            r = self._create(messages=[{"role": "user", "content": ask}], temperature=0.7, max_tokens=1500,
                             seed=1000 + i, extra_body={"reasoning": {"enabled": True}})
            txt = (r.choices[0].message.content or "")
            m = re.findall(r"ANSWER:\s*(Yes|No)", txt, flags=re.I)
            return None if not m else (1.0 if m[-1].lower() == "yes" else 0.0)
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=votes) as ex:
            vs = [v for v in ex.map(one, range(votes)) if v is not None]
        return sum(vs) / len(vs) if vs else 0.5


class MockClient:
    """Scripted stand-in for plumbing tests: random public tool calls, sometimes pastes the reference
    solution (so the full pipeline, including a 'solved' outcome, is exercised without an API key)."""

    def __init__(self, task_dir, cheat_prob=0.35, usage=None, seed=0):
        from pathlib import Path
        self.sol = Path(task_dir) / "solution"
        self.cheat_prob = cheat_prob
        self.usage = usage or Usage()
        self.rng = random.Random(seed)
        self.n = 0

    def chat(self, messages, tools=None, temperature=0.7, max_tokens=0, seed=None):
        self.usage.add(None)
        self.n += 1
        turn = sum(1 for m in messages if m["role"] == "assistant")
        def call(name, args):
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"c{self.n}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}, "mock reasoning"
        if turn == 0:
            return call("run_lint", {})
        if turn >= 4:
            return call("submit", {"summary": "done"})
        if self.rng.random() < self.cheat_prob:
            files = [p for p in self.sol.rglob("*") if p.is_file()]
            f = self.rng.choice(files)
            return call("write_file", {"path": str(f.relative_to(self.sol)), "content": f.read_text()})
        return call(self.rng.choice(["run_synth", "run_lint", "list_files"]), {})

    def p_yes(self, prompt):
        self.usage.add(None)
        return self.rng.random()
