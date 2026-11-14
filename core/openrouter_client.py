"""Ripple through OpenRouter. The key is read from the environment, never printed."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from core.prompts import CLAIM_INSTRUCTIONS, JUDGE_INSTRUCTIONS, PROPOSE_INSTRUCTIONS, ROLLOUT_INSTRUCTIONS
from core.search_types import Action, Claim

DEFAULT_MODEL = "ricursiveai/ripple-65b-preview"
API_URL = "https://openrouter.ai/api/v1/chat/completions"


def load_env_file(path: str | None = None) -> None:
    """Load KEY=VALUE lines into the environment. Does not override existing vars."""
    env_path = Path(path) if path else Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class OpenRouterClient:
    def __init__(self, model: str | None = None):
        load_env_file()
        self.api_key = os.environ.get("OPENROUTER_API_KEY", "")
        chosen = model or os.environ.get("RIPPLE_MODEL") or DEFAULT_MODEL
        if chosen.startswith("your_"):
            chosen = DEFAULT_MODEL
        self.model = chosen
        if not self.api_key or self.api_key.startswith("your_"):
            raise RuntimeError(
                "Set OPENROUTER_API_KEY in .env before a live run. "
                "The example rollout does not need a key."
            )

    def complete(self, system: str, user: str, temperature: float) -> str:
        body = {
            "model": self.model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        req = urllib.request.Request(
            API_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"OpenRouter HTTP {exc.code}: {detail}") from exc
        return payload["choices"][0]["message"]["content"]


def _json_object(text: str) -> dict:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("model did not return JSON")
    return json.loads(text[start : end + 1])


class RippleActor:
    """Three jobs for the same model: propose moves, play a rollout step, judge a state."""

    def __init__(self, client: OpenRouterClient):
        self.client = client

    def propose(self, history: str, tools: list[str], k: int) -> list[Action]:
        user = (
            f"Give {k} different actions.\nTools:\n{tools}\n\nHistory:\n{history}"
        )
        data = _json_object(self.client.complete(PROPOSE_INSTRUCTIONS, user, temperature=0.8))
        actions = []
        for item in data.get("actions", [])[:k]:
            actions.append(
                Action(
                    tool=str(item.get("tool", "")),
                    args=dict(item.get("args") or {}),
                    rationale=str(item.get("rationale", "")),
                )
            )
        return actions

    def rollout_step(self, history: str, tools: list[str]) -> Action:
        data = _json_object(
            self.client.complete(
                ROLLOUT_INSTRUCTIONS,
                f"Tools:\n{tools}\n\nHistory:\n{history}",
                temperature=0.2,
            )
        )
        return Action(
            tool=str(data.get("tool", "")),
            args=dict(data.get("args") or {}),
            rationale=str(data.get("rationale", "")),
        )

    def judge(self, history: str, raw: str) -> tuple[float, str]:
        """Score 0..1. Must quote evidence. This score cannot kill a branch."""
        data = _json_object(
            self.client.complete(
                JUDGE_INSTRUCTIONS,
                f"History:\n{history}\n\nTool output:\n{raw}",
                temperature=0.0,
            )
        )
        score = float(data.get("score", 0.5))
        return max(0.0, min(1.0, score)), str(data.get("evidence", ""))

    def read_claim(self, raw: str, fields: list[str]) -> Claim:
        field_line = ", ".join(fields) if fields else "error_count"
        system = CLAIM_INSTRUCTIONS + f"\nMeasured fields to fill: {field_line}."
        data = _json_object(self.client.complete(system, raw, temperature=0.0))
        conclusion = str(data.pop("conclusion", ""))
        evidence = str(data.pop("evidence", ""))
        return Claim(fields=data, conclusion=conclusion, evidence=evidence)
