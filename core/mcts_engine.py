"""Small MCTS over tool calls.

Ripple proposes moves and can offer a judge guess.
Tools and rules decide whether a branch lives, and how a finished path scores.
The judge guess only sets the starting number in UCB1. Rollouts replace it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from core.search_types import Action, Node
from core.verifier import hard_kill


def ucb1(node: Node, c: float) -> float:
    if node.status == "PRUNED":
        return float("-inf")
    parent = node.parent
    if node.visits <= 0 or parent is None or parent.visits <= 0:
        return float("inf")
    exploit = node.blended_mean()
    explore = c * math.sqrt(math.log(parent.visits) / node.visits)
    return exploit + explore


@dataclass
class TraceStep:
    round_index: int
    phase: str
    node_id: str
    detail: str


@dataclass
class SearchResult:
    root: Node
    best: Node | None
    trace: list[TraceStep] = field(default_factory=list)
    logs: list[dict] = field(default_factory=list)


class ToolSearch:
    """The world the search talks to. Live and scripted worlds both implement this."""

    def propose(self, node: Node, k: int) -> list[Action]:
        raise NotImplementedError

    def act(self, node: Node, action: Action):
        """Return (ToolResult, Claim, judge_score)."""
        raise NotImplementedError

    def rollout(self, node: Node) -> tuple[float, bool, str]:
        """Play the branch out. Return (score, passed, story)."""
        raise NotImplementedError


class MCTS:
    def __init__(
        self,
        world: ToolSearch,
        constraints: dict | None = None,
        k: int = 3,
        rounds: int = 4,
        c: float = 1.41,
    ):
        self.world = world
        self.constraints = constraints or {}
        self.k = k
        self.rounds = rounds
        self.c = c
        self._ids = 0

    def _new_id(self) -> str:
        self._ids += 1
        return f"n{self._ids}"

    def run(self) -> SearchResult:
        root = Node(node_id="root", action=None, status="OPEN", depth=0)
        trace: list[TraceStep] = []
        logs: list[dict] = []
        for round_index in range(1, self.rounds + 1):
            kind, node = self._next(root)
            if kind is None or node is None:
                trace.append(TraceStep(round_index, "stop", root.node_id, "nothing left to try"))
                break
            trace.append(TraceStep(round_index, "select", node.node_id, _path_label(node)))
            if kind == "probe":
                if not self._probe(node, round_index, trace, logs):
                    break
                pending = [
                    child for child in node.children
                    if child.status != "PRUNED" and child.visits <= 0
                ]
                if not pending:
                    continue
                node = max(pending, key=lambda child: child.prior)
                trace.append(TraceStep(round_index, "pick", node.node_id, _path_label(node)))
            self._playout(node, round_index, trace, logs)
        best = _best_child(root)
        return SearchResult(root=root, best=best, trace=trace, logs=logs)

    def _next(self, root: Node) -> tuple[str | None, Node | None]:
        """Probe when a node has no children. Play out one untried child before digging deeper."""
        node = root
        while True:
            if not node.children:
                return "probe", node
            living = [child for child in node.children if child.status != "PRUNED"]
            untried = [child for child in living if child.visits <= 0]
            if untried:
                return "rollout", max(untried, key=lambda child: child.prior)
            expandable = [child for child in living if child.status != "GOAL"]
            if not expandable:
                return None, None
            node = max(expandable, key=lambda child: ucb1(child, self.c))

    def _probe(self, node: Node, round_index: int, trace: list[TraceStep], logs: list[dict]) -> bool:
        """Run only the single next tool call for each idea. Do not play them out."""
        proposals = self.world.propose(node, self.k)
        seen = {child.action.key() for child in node.children if child.action}
        fresh = []
        for action in proposals:
            if action.key() in seen:
                trace.append(TraceStep(round_index, "dedupe", node.node_id, f"dropped repeat {action.tool}"))
                continue
            seen.add(action.key())
            fresh.append(action)
        if not fresh:
            trace.append(TraceStep(round_index, "stop", node.node_id, "no distinct moves"))
            return False
        for action in fresh:
            child = Node(
                node_id=self._new_id(),
                action=action,
                parent=node,
                depth=node.depth + 1,
            )
            result, claim, judge = self.world.act(node, action)
            child.result = result
            child.claim = claim
            child.prior = judge
            node.children.append(child)
            reason = hard_kill(child, self.constraints)
            if reason:
                child.status = "PRUNED"
                child.kill_reason = reason
                self._backup(child, 0.0)
                detail = f"{action.tool}: one call, killed ({reason}). judge {judge:.2f} ignored"
            else:
                detail = f"{action.tool}: one call, still alive. judge guess {judge:.2f}. not played out"
            trace.append(TraceStep(round_index, "probe", child.node_id, detail))
            logs.append(
                {
                    "node": child.node_id,
                    "tool": action.tool,
                    "prior": judge,
                    "status": child.status,
                    "reason": reason,
                }
            )
        return True

    def _playout(self, node: Node, round_index: int, trace: list[TraceStep], logs: list[dict]) -> None:
        score, passed, story = self.world.rollout(node)
        if passed:
            node.status = "GOAL"
        self._backup(node, score)
        trace.append(
            TraceStep(
                round_index,
                "playout",
                node.node_id,
                f"{node.action.tool if node.action else 'root'} played out to {score:.2f}. "
                f"blend {node.blended_mean():.2f}. {story}",
            )
        )
        logs.append({"node": node.node_id, "rollout": score, "status": node.status})

    def _backup(self, node: Node, score: float) -> None:
        cur: Node | None = node
        while cur is not None:
            cur.visits += 1
            cur.value_sum += score
            cur = cur.parent


def _best_child(root: Node) -> Node | None:
    living = [child for child in root.children if child.status != "PRUNED"]
    if not living:
        return None
    return max(living, key=lambda child: (child.blended_mean(), child.visits))


def _path_label(node: Node) -> str:
    names = [n.action.tool for n in node.path() if n.action]
    return "root" if not names else " -> ".join(names)
