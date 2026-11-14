"""Native Verilator and Yosys wrappers for the fast path."""

import re
import subprocess
from pathlib import Path

_VERILATOR_WARNING = re.compile(r"^(%Warning\S*).*$", re.MULTILINE)
_VERILATOR_ERROR = re.compile(r"^(%Error\S*).*$", re.MULTILINE)
_YOSYS_CELL_TOTAL = re.compile(r"Number of cells:\s+(\d+)")
_YOSYS_CELL_LINE = re.compile(r"^\s{2,}(\S+)\s+(\d+)\s*$", re.MULTILINE)
_LATCH_NAME = re.compile(r"LATCH", re.IGNORECASE)


def parse_verilator_log(log: str) -> dict:
    """Extract Verilator %Warning and %Error tags from stderr."""
    warnings = _VERILATOR_WARNING.findall(log or "")
    errors = _VERILATOR_ERROR.findall(log or "")
    return {
        "warnings": warnings,
        "errors": errors,
        "warning_count": len(warnings),
        "error_count": len(errors),
    }


def parse_yosys_stat(log: str) -> dict:
    """Extract per-cell counts and latch totals from a Yosys `stat` log."""
    cells: dict[str, int] = {}
    for name, count in _YOSYS_CELL_LINE.findall(log or ""):
        cells[name] = int(count)
    total_match = _YOSYS_CELL_TOTAL.search(log or "")
    total_cells = int(total_match.group(1)) if total_match else sum(cells.values())
    latch_count = sum(count for name, count in cells.items() if _LATCH_NAME.search(name))
    return {
        "cells": cells,
        "total_cells": total_cells,
        "latch_count": latch_count,
    }


def run_verilator_lint(file_path: str) -> dict:
    """Runs native Verilator linting and returns raw output."""
    cmd = ["verilator", "--lint-only", "-Wall", file_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    parsed = parse_verilator_log(result.stderr)
    return {"status": result.returncode, "log": result.stderr, **parsed}


def run_yosys_synth(file_path: str, recipe: str = "default") -> dict:
    """Runs native Yosys synthesis and returns cell stats."""
    top = Path(file_path).stem
    if recipe == "default":
        script = f"read_verilog -sv {file_path}; synth -top {top}; stat"
    else:
        script = recipe.format(file_path=file_path, top=top)
    cmd = ["yosys", "-p", script]
    result = subprocess.run(cmd, capture_output=True, text=True)
    parsed = parse_yosys_stat(result.stdout)
    return {"status": result.returncode, "log": result.stdout, **parsed}
