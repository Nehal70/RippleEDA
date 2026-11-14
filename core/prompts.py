"""Prompts for Ripple's three jobs. The judge text lives in core/judge.py."""

from __future__ import annotations

from core.judge import JUDGE_INSTRUCTIONS

PROPOSE_INSTRUCTIONS = """
You choose the next chip-design tool calls. Return JSON only:
{"actions":[{"tool":"name","args":{},"rationale":"short"}]}

Give exactly the requested number of actions.
Each action must use a different tool, or the same tool with different args.
Use only tool names from the list you are given.
Do not pick a full place-and-route tool if a lint, simulation, or cell-count tool can answer the question.
""".strip()

ROLLOUT_INSTRUCTIONS = """
You are playing one path out to the end. Pick the single next tool call.
Return JSON only: {"tool":"name","args":{},"rationale":"short"}.
Use only a tool name from the list. Prefer the call that checks whether the task is actually done.
""".strip()

CLAIM_INSTRUCTIONS = """
Read the tool log and fill a claim. Return JSON only.
Put the measured fields first, then "conclusion", then "evidence".
evidence must be a short quote copied from the log.
conclusion is one of: pass, fail, hung, edited, counted, unknown.
Do not invent numbers that are not in the log.
""".strip()
