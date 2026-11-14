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
    return cap_violation(result.truth, constraints)


# ---------------------------------------------------------------------------
# Terminal score
# ---------------------------------------------------------------------------

# Score bands. A task spec may override any of these through `weights`.
# Ordering must hold: crash <= fail_floor <= partial_ceiling < pass_floor <= 1.
DEFAULT_WEIGHTS: dict = {
    # Tool crashed, compile error, banned tool, or a hang with nothing measured.
    "crash": 0.05,
    # Testbench ran and zero checks passed.
    "fail_floor": 0.10,
    # Testbench ran and almost every check passed (never reached: all-pass moves up a band).
    "partial_ceiling": 0.50,
    # Every check passed and the PPA proxies are twice the baseline or worse.
    "pass_floor": 0.60,
    # Relative weight of each PPA proxy among passing designs. Missing proxies are
    # dropped and the rest renormalised, so the sum does not have to be 1.
    "ppa": {"area": 0.5, "cycles": 0.3, "delay": 0.2},
    # Subtracted per tool call on the branch, inside the partial and pass tiers only,
    # and never below the tier floor. Small on purpose: it ranks two correct answers
    # by how many calls they needed (sim-before-synth beats a formal detour), but it
    # can never push a passing design under a failing one. The step cap in the
    # engine is the budget; this is only the preference inside it.
    "step_penalty": 0.02,
}

# PPA proxy -> (truth keys to try in order, constraint key for the hard cap).
# `cell_area` is the `stat -liberty` area in um^2; `total_cells` is the plain cell count.
# `delay` is critical path from `yosys ... ; ltp` (levels) or OpenSTA (ns); any unit works
# because every proxy is a ratio against the baseline measured with the same tool.
PPA_PROXIES: dict[str, tuple[tuple[str, ...], str]] = {
    "area": (("cell_area", "total_cells"), "max_cells"),
    "cycles": (("cycles",), "max_cycles"),
    "delay": (("critical_path_ns", "ltp_levels", "delay"), "max_delay"),
}


def _merge_weights(weights: dict | None) -> dict:
    merged = {**DEFAULT_WEIGHTS, **(weights or {})}
    merged["ppa"] = {**DEFAULT_WEIGHTS["ppa"], **((weights or {}).get("ppa") or {})}
    bands = (merged["crash"], merged["fail_floor"], merged["partial_ceiling"], merged["pass_floor"])
    if not (0.0 <= bands[0] <= bands[1] <= bands[2] < bands[3] <= 1.0):
        raise ValueError(f"score bands must satisfy crash<=fail_floor<=partial_ceiling<pass_floor<=1, got {bands}")
    return merged


def _num(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first(d: dict, keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = _num(d.get(key))
        if value is not None:
            return value
    return None


def correctness_fraction(truth: dict) -> float | None:
    """Fraction of testbench checks passed, or None if the testbench never reported.

    Prefers `tests_passed`/`tests_total`. Falls back to the boolean `bug_fixed`
    (1.0 or 0.0) when only a single verdict is available.
    """
    total = _num(truth.get("tests_total"))
    passed = _num(truth.get("tests_passed"))
    if total and passed is not None:
        return max(0.0, min(1.0, passed / total))
    fixed = truth.get("bug_fixed")
    if isinstance(fixed, bool):
        return 1.0 if fixed else 0.0
    return None


def cap_violation(truth: dict, constraints: dict | None = None) -> str:
    """Hard kill rules that also apply at the end of a rollout. Empty string means fine."""
    constraints = constraints or {}
    if constraints.get("forbid_latches") and int(truth.get("latch_count") or 0) > 0:
        return "latch inferred"
    for name, (keys, cap_key) in PPA_PROXIES.items():
        cap = _num(constraints.get(cap_key))
        value = _first(truth, keys)
        if cap is not None and value is not None and value > cap:
            return f"{name} {value:g} over cap {cap:g}"
    return ""


def ppa_term(value: float, baseline: float | None, cap: float | None) -> float | None:
    """One PPA proxy mapped to 0..1 where higher is better.

    With a baseline: improvement = 1 - value/baseline (DeLorenzo et al. 2024, eq. 1),
    anchored so matching the baseline scores 0.5, eliminating the metric scores 1.0,
    and doubling it scores 0.0. The anchor keeps "correct but a bit bigger than the
    starting design" ordered above "correct but twice as big", which a plain
    clamp(1 - ratio) would flatten to 0 for both.
    Without a baseline but with a cap: fall back to 1 - value/cap.
    """
    if baseline is not None and baseline > 0:
        improvement = 1.0 - value / baseline
        return max(0.0, min(1.0, 0.5 + 0.5 * improvement))
    if cap is not None and cap > 0:
        return max(0.0, min(1.0, 1.0 - value / cap))
    return None


def ppa_score(truth: dict, constraints: dict, baseline: dict, ppa_weights: dict) -> float:
    """Weighted mean of the available PPA proxy terms. 0.5 when nothing is measurable."""
    weighted = 0.0
    weight_sum = 0.0
    for name, (keys, cap_key) in PPA_PROXIES.items():
        weight = _num(ppa_weights.get(name)) or 0.0
        if weight <= 0:
            continue
        value = _first(truth, keys)
        if value is None:
            continue
        term = ppa_term(value, _first(baseline, keys), _num(constraints.get(cap_key)))
        if term is None:
            continue
        weighted += weight * term
        weight_sum += weight
    return weighted / weight_sum if weight_sum > 0 else 0.5


def terminal_score(
    truth: dict,
    constraints: dict | None = None,
    baseline: dict | None = None,
    weights: dict | None = None,
    steps: int = 0,
) -> tuple[float, bool]:
    """Score a finished rollout. Returns (score in 0..1, passed).

    Shape (correctness gates, PPA ranks, steps break ties):

        killed   -> 0.0                       cap exceeded or forbidden latch
        crash    -> w.crash                   compile error / banned tool / hang with no checks
        partial  -> w.fail_floor + (w.partial_ceiling - w.fail_floor) * c  - steps * w.step_penalty
        pass     -> w.pass_floor + (1 - w.pass_floor) * ppa                - steps * w.step_penalty

    The step term is clamped so a score never drops below its tier floor.

    where c is the fraction of testbench checks passed (c < 1) and ppa is the weighted
    mean over {area, cycles, delay} of clamp(0.5 + 0.5 * (1 - value/baseline)).
    Because partial_ceiling < pass_floor, nothing that fails verification can outscore
    anything that passes, no matter how small it is.

    Why this shape, in plain words:

    * Correctness first, then PPA, as a strict tier split. Every published RTL benchmark
      scores this way: RTLLM / RTLLM 2.0 check syntax, then functionality, then compare
      PPA only for designs that pass (Lu, Liu, Zhang, Xie, "RTLLM", ASP-DAC 2024,
      arXiv:2308.05345; Liu et al. "OpenLLM-RTL", ICCAD 2024, arXiv:2503.15112).
      VerilogEval v1/v2 and CVDP score functional pass only (Liu et al. ICCAD 2023,
      arXiv:2309.07544; Pinckney et al. 2024, arXiv:2408.11053; Pinckney et al.
      "CVDP", 2025, arXiv:2506.14074). The closest precedent, MCTS over Verilog with
      a synthesised-PPA reward, uses exactly a tiered reward: a penalty for
      non-compilable, a smaller penalty for non-functional, and a positive base plus a
      PPA bonus for functional designs (DeLorenzo, Chowdhury, Gohil, Thakur, Garg,
      Karri, Rajendran, "Make Every Move Count: LLM-based High-Quality RTL Code
      Generation Using MCTS", 2024, arXiv:2402.03289, eq. 1, alpha_NC=-1,
      alpha_NF=-0.1, alpha_B=0.5). RTLRewriter's C-MCTS also tiers its node value:
      0 if verification fails, 0.5 if equivalent but not better than the original,
      1 if equivalent and better (Yao et al., ICCAD 2024, arXiv:2409.11414, eq. 6).

    * Partial credit from the fraction of checks passed, instead of one boolean.
      EvolVE scores generation nodes in its MCTS by pass rate p_i/|T| with a large
      negative constant for compile or simulation failure, and reports that the dense
      signal speeds convergence (Chen et al., "EvolVE", 2026, arXiv:2601.18067, eq. 3).
      VeriPrefer ranks candidates by number of test cases passed (Wang et al.,
      arXiv:2504.15804). VeriReason instead keeps correctness binary and fills the gap
      with an AST-similarity term (arXiv:2505.11849); pass fraction is the cheaper,
      tool-grounded version of that idea. VeriPrefer warns that a bare pass-fraction
      reward invited reward hacking in RL training; here the fraction never crosses
      the pass gate, so the only way up is to pass everything.

    * PPA normalised against a baseline, not just a cap. DeLorenzo et al. use
      1 - (a*d)/(a_ref*d_ref) with a_ref, d_ref measured on the first synthesised
      netlist of the same module; RTLRewriter compares against the original RTL;
      RTLLM compares against the human reference; EvolVE reports improvement ratio
      versus the baseline solution. Caps stay as kill rules (and as the fallback
      normaliser when no baseline exists).

    * Fast proxies only. DeLorenzo et al. and EvolVE both get area and delay from
      Yosys and run Icarus Verilog for function; EvolVE measured a >30x speedup versus
      VCS + Design Compiler and argue area tracks power closely enough to skip power.
      ChipGPT and RTLLM use Design Compiler area (um^2) / power / timing, which is
      out of budget here. So: `yosys stat` cells (or liberty area), testbench cycles,
      and optional `ltp` / OpenSTA critical path.

    * Weights from the task spec. Benchmarks disagree on the mix (DeLorenzo: area x
      delay product; EvolVE: area x latency; ChipGPT: whichever target the user
      picks), so the mix is per task with DEFAULT_WEIGHTS as the documented default.

    Arguments:
        truth: final tool measurements. Recognised keys: error_count, sim_hung,
            banned, tests_passed, tests_total, bug_fixed, latch_count, and the PPA
            keys in PPA_PROXIES (total_cells / cell_area, cycles, critical path).
        constraints: task caps (max_cells, max_cycles, max_delay, forbid_latches).
            May also carry "baseline" and "weights" if the caller does not pass them.
        baseline: same keys as truth, measured on the design as given at task start.
        weights: overrides for DEFAULT_WEIGHTS (bands and the "ppa" sub-dict).
    """
    constraints = constraints or {}
    baseline = baseline if baseline is not None else (constraints.get("baseline") or {})
    w = _merge_weights(weights if weights is not None else constraints.get("weights"))

    if cap_violation(truth, constraints):
        return 0.0, False

    broken = (
        truth.get("banned") is True
        or int(truth.get("error_count") or 0) > 0
    )
    fraction = correctness_fraction(truth)
    hung = truth.get("sim_hung") is True

    if broken or fraction is None:
        return w["crash"], False

    penalty = max(0, int(steps)) * float(w.get("step_penalty") or 0.0)
    if hung or fraction < 1.0:
        # A hung testbench never finished its checks, so it is at best partial.
        raw = w["fail_floor"] + (w["partial_ceiling"] - w["fail_floor"]) * fraction
        return max(w["fail_floor"], raw - penalty), False

    ppa = ppa_score(truth, constraints, baseline, w["ppa"])
    raw = w["pass_floor"] + (1.0 - w["pass_floor"]) * ppa
    return max(w["pass_floor"], raw - penalty), True
