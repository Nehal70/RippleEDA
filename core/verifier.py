"""Tool truth decides life or death. The judge is not allowed to kill a branch."""

from __future__ import annotations

from core.search_types import Claim, Node, ToolResult

# Full place-and-route is too slow to sit inside the search.
BANNED_IN_SEARCH = {
    "openlane_run_flow",
    "openlane_run_synthesis",
    "openlane_run_floorplan",
    "openlane_run_placement",
    "openlane_run_cts",
    "openlane_run_routing",
}


def claim_mismatches(claim: Claim | None, result: ToolResult | None) -> list[str]:
    if claim is None or result is None:
        return []
    bad = []
    for key, truth in result.truth.items():
        if key not in claim.fields:
            continue
        if claim.fields[key] != truth:
            bad.append(f"{key}: claim={claim.fields[key]!r} truth={truth!r}")
    return bad


def hard_kill(node: Node, constraints: dict | None = None) -> str:
    """Return a reason string if the branch must die. Empty string means it lives.

    A judge score never reaches this function.
    """
    constraints = constraints or {}
    action = node.action
    if action is not None and action.tool in BANNED_IN_SEARCH:
        return f"banned tool inside search: {action.tool}"
    result = node.result
    if result is None:
        return ""
    if result.banned:
        return "tool marked banned"
    if not result.ok:
        return "tool failed"
    mismatches = claim_mismatches(node.claim, result)
    if mismatches:
        return "misread tool output: " + "; ".join(mismatches)
    truth = result.truth
    if constraints.get("forbid_latches") and int(truth.get("latch_count") or 0) > 0:
        return "latch inferred"
    max_cells = constraints.get("max_cells")
    cells = truth.get("total_cells")
    if max_cells is not None and cells is not None and int(cells) > int(max_cells):
        return f"cell count {cells} over cap {max_cells}"
    max_cycles = constraints.get("max_cycles")
    cycles = truth.get("cycles")
    if max_cycles is not None and cycles is not None and int(cycles) > int(max_cycles):
        return f"cycles {cycles} over cap {max_cycles}"
    return ""


def terminal_score(truth: dict, constraints: dict | None = None) -> tuple[float, bool]:
    """Score a finished rollout. Returns (score 0..1, passed)."""
    constraints = constraints or {}
    if truth.get("bug_fixed") is not True:
        return 0.15, False
    if truth.get("sim_hung") is True:
        return 0.1, False
    score = 0.7
    cells = truth.get("total_cells")
    cap = constraints.get("max_cells")
    if cells is not None and cap:
        score += 0.2 * max(0.0, 1.0 - (int(cells) / int(cap)))
    cycles = truth.get("cycles")
    max_cycles = constraints.get("max_cycles")
    if cycles is not None and max_cycles:
        score += 0.1 * max(0.0, 1.0 - (int(cycles) / int(max_cycles)))
    return min(1.0, score), True
