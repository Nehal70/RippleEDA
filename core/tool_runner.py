"""Run the tools the search can actually execute, plus the slow ones it must refuse."""

from __future__ import annotations

from pathlib import Path

from core.search_types import Action, ToolResult
from core.verifier import BANNED_IN_SEARCH
from tools.eda_tools_native import run_verilator_lint, run_yosys_synth

# Names the model is allowed to choose. Most match mcp4eda.
TOOL_MENU = [
    "verible_lint",
    "verilator_compile",
    "verilator_simulate",
    "yosys_analyze",
    "yosys_synth",
    "yosys_show",
    "gtkwave_open",
    "openlane_run_flow",
]


def execute(action: Action, design_path: str) -> ToolResult:
    if action.tool in BANNED_IN_SEARCH:
        return ToolResult(
            ok=False,
            raw=f"refused: {action.tool} is outside the search budget",
            truth={},
            banned=True,
        )
    path = str(action.args.get("filepath") or action.args.get("design") or design_path)
    if action.tool in {"verible_lint", "verilator_compile", "verilator_simulate"}:
        return _from_verilator(path)
    if action.tool in {"yosys_analyze", "yosys_synth", "yosys_show"}:
        return _from_yosys(path)
    return ToolResult(
        ok=False,
        raw=f"{action.tool} is on the menu but not executable in this harness yet",
        truth={"error_count": 1},
    )


def _from_verilator(path: str) -> ToolResult:
    if not Path(path).exists():
        return ToolResult(False, f"file not found: {path}", {"error_count": 1})
    try:
        parsed = run_verilator_lint(path)
    except FileNotFoundError:
        return ToolResult(False, "verilator is not on PATH", {"error_count": 1})
    truth = {
        "error_count": parsed.get("error_count", 0),
        "warning_count": parsed.get("warning_count", 0),
    }
    return ToolResult(parsed.get("status", 1) == 0, parsed.get("log", ""), truth)


def _from_yosys(path: str) -> ToolResult:
    if not Path(path).exists():
        return ToolResult(False, f"file not found: {path}", {"error_count": 1})
    try:
        parsed = run_yosys_synth(path)
    except FileNotFoundError:
        return ToolResult(False, "yosys is not on PATH", {"error_count": 1})
    truth = {
        "total_cells": parsed.get("total_cells", 0),
        "latch_count": parsed.get("latch_count", 0),
        "error_count": 0 if parsed.get("status", 1) == 0 else 1,
    }
    return ToolResult(parsed.get("status", 1) == 0, parsed.get("log", ""), truth)
