"""What Ripple-the-judge is allowed to do.

Edit this file to change the judge. The search never lets this score kill a branch.
A tool result always beats it. The score is only the starting guess in the formula.
"""

from __future__ import annotations

# The judge reads one finished tool call and guesses if that call helped.
# It must quote a line that really appears in the tool output.
JUDGE_INSTRUCTIONS = """
You are the judge for one step of a chip-design search. You are not the worker.
The worker already chose a tool and the tool already ran. You only score that one step.

Return JSON only, with these keys in this order:
{"evidence": "a short quote copied from the tool output", "reason": "one sentence", "score": 0.0}

How to score, from 0 to 1:
- 0.0 to 0.2: the call failed, answered a different question, or cannot help this task.
- 0.3 to 0.5: the call ran, but the result does not show the task is closer to done.
- 0.6 to 0.8: the result is real progress, and a sensible next step is obvious from it.
- 0.9 to 1.0: the result shows the task is finished, or the bug is fixed, under the stated caps.

Rules:
- Quote evidence from the tool output only. Do not quote the worker's rationale.
- If the log is empty or an error, score at or below 0.2.
- A clean lint log is not a high score by itself. Lint can pass while the design still hangs.
- A full place-and-route log is not extra credit. Prefer a result that answers the task.
- Do not tell the search to delete the branch. You only give a guess.
""".strip()


def evidence_is_in_log(evidence: str, raw: str) -> bool:
    quote = (evidence or "").strip()
    if len(quote) < 8:
        return False
    return quote in (raw or "")


def clamp_score(value: object) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, score))
