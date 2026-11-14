"""Measure the judge against what the tools later said, and set lambda (prior_strength) from data.

Every node gets a judge guess (Node.prior) when it is created, and rollout scores later. This logs both,
reports whether the guess predicted the outcome, and fits the prior_strength used by Node.blended_mean():

    blend = (1 - lam) * prior + lam * rollout_mean,   lam = visits / (visits + prior_strength)

Usage (no change to the engine):
    log = JudgeLog()
    mcts = MCTS(world, ...); log.attach(mcts); result = mcts.run()
    log.save("results/judge_log.jsonl")
    python -m core.judge_log results/judge_log.jsonl        # report + fitted prior_strength
"""

from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path


class JudgeLog:
    def __init__(self):
        self.records: list[dict] = []

    def attach(self, mcts) -> "JudgeLog":
        """Wrap mcts._playout and mcts._probe so every play-out and every killed probe is recorded."""
        orig_playout, orig_probe = mcts._playout, mcts._probe
        log = self

        def playout(node, round_index, trace, logs):
            before = node.value_sum
            orig_playout(node, round_index, trace, logs)
            log.records.append({"node": node.node_id, "depth": node.depth,
                                "tool": node.action.tool if node.action else "root",
                                "prior": node.prior, "rollout": node.value_sum - before,
                                "passed": node.status == "GOAL", "visit": node.visits, "t": time.time()})

        def probe(node, round_index, trace, logs):
            n_before = len(node.children)
            ok = orig_probe(node, round_index, trace, logs)
            for child in node.children[n_before:]:
                if child.status == "PRUNED":       # tools killed it: outcome 0 regardless of the judge
                    log.records.append({"node": child.node_id, "depth": child.depth,
                                        "tool": child.action.tool if child.action else "?", "prior": child.prior,
                                        "rollout": 0.0, "passed": False, "visit": 1, "pruned": True,
                                        "reason": child.kill_reason, "t": time.time()})
            return ok

        mcts._playout, mcts._probe = playout, probe
        return self

    def save(self, path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            for r in self.records:
                f.write(json.dumps(r) + "\n")

    @staticmethod
    def load(*paths) -> list[dict]:
        out = []
        for path in paths:
            for line in Path(path).read_text().splitlines():
                if line.strip():
                    out.append(json.loads(line))
        return out


# ----------------------------------------------------------------------------- analysis
def _auroc(scores, labels):
    pos = [s for s, y in zip(scores, labels) if y]
    neg = [s for s, y in zip(scores, labels) if not y]
    if not pos or not neg:
        return float("nan")
    return sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / (len(pos) * len(neg))


def _pearson(x, y):
    n = len(x)
    if n < 3:
        return float("nan")
    mx, my = sum(x) / n, sum(y) / n
    sx = math.sqrt(sum((a - mx) ** 2 for a in x))
    sy = math.sqrt(sum((b - my) ** 2 for b in y))
    return float("nan") if sx == 0 or sy == 0 else sum((a - mx) * (b - my) for a, b in zip(x, y)) / (sx * sy)


def _ece(scores, targets, bins=10):
    n, tot = len(scores), 0.0
    for b in range(bins):
        idx = [i for i, s in enumerate(scores) if b / bins <= s < (b + 1) / bins or (b == bins - 1 and s == 1.0)]
        if idx:
            tot += len(idx) / n * abs(sum(scores[i] for i in idx) / len(idx) - sum(targets[i] for i in idx) / len(idx))
    return tot


def judge_report(records: list[dict]) -> dict:
    """How well did the judge's first guess predict the node's first real outcome?"""
    first = {}
    for r in sorted(records, key=lambda r: (r["node"], r.get("visit", 1))):
        first.setdefault(r["node"], r)
    rs = list(first.values())
    pri = [r["prior"] for r in rs]
    out = [r["rollout"] for r in rs]
    return {"nodes": len(rs), "pearson_prior_vs_rollout": _pearson(pri, out),
            "auroc_prior_vs_passed": _auroc(pri, [r["passed"] for r in rs]),
            "brier_prior_vs_rollout": sum((p - o) ** 2 for p, o in zip(pri, out)) / max(1, len(rs)),
            "ece_prior_vs_rollout": _ece(pri, out) if rs else float("nan"),
            "mean_prior": sum(pri) / max(1, len(pri)), "mean_rollout": sum(out) / max(1, len(out))}


def fit_prior_strength(records: list[dict], grid=(0.0, 0.25, 0.5, 1.0, 1.5, 2.0, 3.0, 5.0, 8.0, 13.0)) -> dict:
    """Pick prior_strength minimising the error of the blended estimate at predicting the NEXT rollout,
    exactly as Node.blended_mean() would have produced it before that rollout arrived."""
    seqs = {}
    for r in sorted(records, key=lambda r: (r["node"], r.get("visit", 1), r.get("t", 0))):
        if r.get("pruned"):
            continue
        seqs.setdefault(r["node"], {"prior": r["prior"], "rollouts": []})["rollouts"].append(r["rollout"])
    best, table = None, []
    for ps in grid:
        err = n = 0
        for s in seqs.values():
            done = []
            for nxt in s["rollouts"]:
                v = len(done)
                lam = v / (v + ps) if (v + ps) > 0 else 1.0
                pred = s["prior"] if v == 0 else (1 - lam) * s["prior"] + lam * (sum(done) / v)
                err += (pred - nxt) ** 2
                n += 1
                done.append(nxt)
        mse = err / max(1, n)
        table.append((ps, mse))
        if best is None or mse < best[1]:
            best = (ps, mse)
    return {"prior_strength": best[0] if best else 1.0, "mse": best[1] if best else float("nan"),
            "grid": table, "nodes": len(seqs), "predictions": sum(len(s["rollouts"]) for s in seqs.values())}


if __name__ == "__main__":
    recs = JudgeLog.load(*sys.argv[1:])
    print(json.dumps({"judge": judge_report(recs), "lambda_fit": fit_prior_strength(recs)}, indent=2))
