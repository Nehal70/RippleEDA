"""Monte Carlo tree search state engine with a hybrid process reward model."""

import math
import re

_LATCH_NAME = re.compile(r"LATCH", re.IGNORECASE)


class Node:
    def __init__(self, node_id, rtl_content, parent=None):
        self.node_id = node_id
        self.rtl_content = rtl_content
        self.parent = parent
        self.children = []
        self.visits = 0
        self.value_sum = 0.0
        self.prm_score = 0.0
        self.status = "ROOT"  # ROOT, EXPANDED, PRUNED, BACKTRACKED, GOAL


def _latch_count(tool_output: dict) -> int:
    if "latch_count" in tool_output:
        return int(tool_output["latch_count"])
    cells = tool_output.get("cells") or {}
    return sum(count for name, count in cells.items() if _LATCH_NAME.search(str(name)))


def evaluate_prm(node: Node, tool_output: dict, constraints: dict) -> float:
    """
    Hybrid Judge: Uses deterministic checks (e.g. latch_count) +
    LLM API calls to score state transitions (0.0 to 1.0).

    Deterministic penalties applied here:
    - non-zero tool status
    - Verilator %Error / %Warning counts
    - latch_count against forbid_latches or max_latches
    - total_cells against max_cells
    The LLM judge is not called yet; missing model output leaves the
    deterministic score unchanged.
    """
    constraints = constraints or {}
    score = 1.0

    if tool_output.get("status", 0) != 0:
        score -= 0.5

    error_count = tool_output.get("error_count")
    if error_count is None:
        error_count = len(tool_output.get("errors") or [])
    warning_count = tool_output.get("warning_count")
    if warning_count is None:
        warning_count = len(tool_output.get("warnings") or [])

    if error_count:
        score -= min(0.5, 0.15 * error_count)
    if warning_count:
        score -= min(0.2, 0.02 * warning_count)

    latch_count = _latch_count(tool_output)
    max_latches = constraints.get("max_latches")
    if constraints.get("forbid_latches") and latch_count > 0:
        score -= 0.4
    elif max_latches is not None and latch_count > max_latches:
        score -= 0.3

    total_cells = tool_output.get("total_cells")
    max_cells = constraints.get("max_cells")
    if max_cells is not None and total_cells is not None and total_cells > max_cells:
        score -= 0.2

    score = max(0.0, min(1.0, score))
    node.prm_score = score
    return score


def calculate_ucb1(node: Node, c: float = 1.41) -> float:
    """Calculates UCB1 for node selection."""
    if node.visits == 0 or node.parent is None or node.parent.visits <= 0:
        return float("inf")
    return (node.value_sum / node.visits) + c * math.sqrt(
        math.log(node.parent.visits) / node.visits
    )
