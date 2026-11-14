"""A tiny fake chip task so the search can be walked through with no API key.

The design hangs when the receiver stalls. Lint looks clean. A full
place-and-route run is on the menu and is banned inside the search.
One edit fixes the hang. Another edit fixes it by adding a lot of cells.
"""

from __future__ import annotations

from core.mcts_engine import ToolSearch
from core.search_types import Action, Claim, Node, ToolResult

CONSTRAINTS = {"forbid_latches": True, "max_cells": 100, "max_cycles": 4}


def _done(node: Node) -> set[str]:
    return {n.action.tool for n in node.path() if n.action}


class ScriptedChipWorld(ToolSearch):
    def propose(self, node: Node, k: int) -> list[Action]:
        done = _done(node)
        if "verible_lint" in done and "verilator_simulate" not in done and "edit_fsm" not in done:
            options = [
                Action("verilator_simulate", {}, "lint was clean, so try the stall in simulation"),
                Action("yosys_analyze", {"analysisType": "stats"}, "count cells even though nothing changed"),
                Action("gtkwave_open", {}, "open a waveform"),
            ]
        elif "edit_fsm" in done:
            options = [
                Action("verilator_simulate", {}, "check the hang is gone"),
                Action("yosys_analyze", {"analysisType": "stats"}, "count cells"),
                Action("openlane_run_flow", {}, "run the full flow to be sure"),
            ]
        elif "verilator_simulate" in done and "edit_fsm" not in done and "edit_extra_regs" not in done:
            options = [
                Action("edit_fsm", {}, "hold the response until RREADY returns"),
                Action("edit_extra_regs", {}, "add a skid buffer of extra registers"),
                Action("gtkwave_open", {}, "open the waveform"),
            ]
        else:
            options = [
                Action("verible_lint", {}, "lint first, it is the usual start"),
                Action("openlane_run_flow", {}, "run place and route for a real answer"),
                Action("verilator_simulate", {}, "simulate the stall case"),
            ]
        return options[:k]

    def act(self, node: Node, action: Action):
        done = _done(node)
        done.add(action.tool)
        fixed = "edit_fsm" in done or "edit_extra_regs" in done
        cells = 140 if "edit_extra_regs" in done else (80 if fixed else 70)
        if action.tool == "openlane_run_flow":
            result = ToolResult(False, "refused: full flow is outside the search budget", {}, banned=True)
            claim = Claim({}, "skip", "")
            return result, claim, 0.9  # judge likes the idea; the verifier still kills it
        if action.tool == "verible_lint":
            result = ToolResult(True, "lint clean, 0 errors", {"error_count": 0, "sim_hung": True})
            claim = Claim({"error_count": 0}, "looks fine", "lint clean, 0 errors")
            return result, claim, 0.82
        if action.tool == "verilator_simulate":
            hung = not fixed
            raw = "SIM HUNG waiting on RREADY" if hung else "SIM PASS in 2 cycles"
            result = ToolResult(
                True,
                raw,
                {"sim_hung": hung, "bug_fixed": not hung, "cycles": 2, "total_cells": cells},
            )
            claim = Claim({"sim_hung": hung, "cycles": 2}, "hung" if hung else "pass", raw)
            return result, claim, 0.55 if hung else 0.9
        if action.tool == "edit_fsm":
            result = ToolResult(True, "edited ready/valid hold", {"bug_fixed": False, "total_cells": 80})
            claim = Claim({"total_cells": 80}, "edited", "edited ready/valid hold")
            return result, claim, 0.7
        if action.tool == "edit_extra_regs":
            result = ToolResult(True, "added 3 register stages", {"bug_fixed": False, "total_cells": 140})
            claim = Claim({"total_cells": 140}, "edited", "added 3 register stages")
            return result, claim, 0.6
        if action.tool == "yosys_analyze":
            cells = 140 if "edit_extra_regs" in done else (80 if "edit_fsm" in done else 70)
            result = ToolResult(True, f"Number of cells: {cells}", {"total_cells": cells})
            claim = Claim({"total_cells": cells}, "counted", f"Number of cells: {cells}")
            return result, claim, 0.4
        result = ToolResult(True, "waveform opened", {})
        claim = Claim({}, "looking", "waveform opened")
        return result, claim, 0.3

    def rollout(self, node: Node) -> tuple[float, bool, str]:
        done = _done(node)
        if "edit_extra_regs" in done:
            return 0.35, False, "hang gone, but 140 cells is over the cap of 100"
        if "edit_fsm" in done or (
            node.action and node.action.tool == "verilator_simulate" and "edit_fsm" in done
        ):
            return 0.92, True, "hang gone, 80 cells, 2 cycles"
        if node.action and node.action.tool == "verilator_simulate":
            return 0.2, False, "played out from the sim: still needs an edit, this sample did not finish"
        return 0.15, False, "played out from here and never fixed the hang"
