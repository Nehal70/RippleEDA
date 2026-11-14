"""Live search world. Ripple proposes, judges, and plays paths out. Tools measure."""

from __future__ import annotations

from core.judge import clamp_score, evidence_is_in_log
from core.mcts_engine import ToolSearch
from core.openrouter_client import RippleActor
from core.search_types import Action, Claim, Node, ToolResult
from core.tool_runner import TOOL_MENU, execute
from core.verifier import terminal_score


class LiveChipWorld(ToolSearch):
    def __init__(
        self,
        actor: RippleActor,
        task: str,
        design_path: str,
        constraints: dict | None = None,
        rollout_steps: int = 2,
    ):
        self.actor = actor
        self.task = task
        self.design_path = design_path
        self.constraints = constraints or {}
        self.rollout_steps = rollout_steps

    def propose(self, node: Node, k: int) -> list[Action]:
        return self.actor.propose(self._history(node), TOOL_MENU, k)

    def act(self, node: Node, action: Action):
        result = execute(action, self.design_path)
        fields = [key for key in result.truth.keys()]
        claim = self.actor.read_claim(result.raw or result.truth.__repr__(), fields)
        judge, evidence = self.actor.judge(self._history(node), result.raw)
        if not evidence_is_in_log(evidence, result.raw):
            judge = 0.5
        return result, claim, clamp_score(judge)

    def rollout(self, node: Node) -> tuple[float, bool, str]:
        """Keep calling Ripple for a few steps. The score comes from the last tool truth."""
        story = []
        latest = node.result.truth if node.result else {}
        history_node = node
        for _ in range(self.rollout_steps):
            action = self.actor.rollout_step(self._history(history_node), TOOL_MENU)
            result = execute(action, self.design_path)
            story.append(f"{action.tool} -> {'ok' if result.ok else 'fail'}")
            latest = {**latest, **result.truth}
            if result.banned or not result.ok:
                break
            history_node = Node(
                node_id="roll",
                action=action,
                parent=history_node,
                result=result,
                depth=history_node.depth + 1,
            )
        score, passed = terminal_score(latest, self.constraints)
        if not passed and latest.get("error_count", 1) == 0:
            score = max(score, 0.35)
        return score, passed, "; ".join(story) or "no further calls"

    def _history(self, node: Node) -> str:
        lines = [f"Task: {self.task}", f"Design: {self.design_path}"]
        for step in node.path():
            if step.action is None:
                continue
            raw = ""
            if step.result is not None:
                raw = (step.result.raw or "")[:500]
            lines.append(f"Called {step.action.tool} {step.action.args}. Output: {raw}")
        return "\n".join(lines)
