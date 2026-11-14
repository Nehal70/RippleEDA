"""Shared types for the tool-call search tree."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Action:
    tool: str
    args: dict[str, Any]
    rationale: str

    def key(self) -> str:
        items = tuple(sorted((str(k), str(v)) for k, v in self.args.items()))
        return f"{self.tool}|{items}"


@dataclass
class ToolResult:
    ok: bool
    raw: str
    truth: dict[str, Any]
    banned: bool = False


@dataclass
class Claim:
    fields: dict[str, Any]
    conclusion: str
    evidence: str


@dataclass
class Node:
    node_id: str
    action: Action | None
    parent: Node | None = None
    children: list[Node] = field(default_factory=list)
    visits: int = 0
    value_sum: float = 0.0
    prior: float = 0.5
    status: str = "OPEN"  # OPEN, PRUNED, GOAL
    kill_reason: str = ""
    claim: Claim | None = None
    result: ToolResult | None = None
    depth: int = 0

    def path(self) -> list[Node]:
        nodes = []
        cur: Node | None = self
        while cur is not None:
            nodes.append(cur)
            cur = cur.parent
        return list(reversed(nodes))

    def rollout_mean(self) -> float | None:
        if self.visits <= 0:
            return None
        return self.value_sum / self.visits

    def blended_mean(self, prior_strength: float = 1.0) -> float:
        """Judge sets the starting guess. Real rollouts take over as they arrive."""
        if self.visits <= 0:
            return self.prior
        lam = self.visits / (self.visits + prior_strength)
        return (1.0 - lam) * self.prior + lam * (self.value_sum / self.visits)
