"""
Three ways to drive the same policy (Ripple-65B) on the same public prompt + tools:

  linear  : one ReAct trajectory (the "plain Ripple" baseline)
  bestofn : N independent linear trajectories, pick the one the PRM likes best (compute-matched baseline)
  mcts    : PRM-guided Monte Carlo tree search over agent turns (each node = one LLM turn + its tool calls)

Every method is finally scored by the HIDDEN grader (eda.grade) — the agent never sees it.
"""
from __future__ import annotations

import json
import math
import random
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from eda.grade import grade
from agent.workspace import TOOL_SCHEMAS, Workspace

SYSTEM = """You are a senior RTL design and verification engineer working in a sandbox.
You can read/write files in the workspace and run real EDA tools (Verilator lint, Yosys
synthesis to sky130hd, OpenSTA, Icarus simulation, SymbiYosys formal, cocotb, equivalence
checking, and a shell). Work step by step: inspect, form hypotheses, gather evidence with
the tools (write your own testbenches / properties when needed), edit, and re-check.
Edit files by writing their complete new content. When you are confident the task is
fully done, call submit. Your submission is graded by a hidden signoff suite."""


def _user_msg(spec):
    return f"{spec['prompt'].strip()}\n\nWorkspace files: use list_files / read_file to inspect them."


def _compact(messages, keep_full=12, clip=400):
    """Keep context bounded: older tool outputs are truncated (system/user kept verbatim)."""
    out = []
    n = len(messages)
    for i, m in enumerate(messages):
        if m["role"] == "tool" and i < n - keep_full and len(str(m["content"])) > clip:
            m = {**m, "content": str(m["content"])[:clip] + " ...[older output truncated]"}
        out.append(m)
    return out


def _act(client, ws, messages, temperature, seed=None):
    """One agent turn: LLM call + execute its tool calls. Returns (new_messages, info)."""
    msg, reasoning = client.chat(_compact(messages), tools=TOOL_SCHEMAS, temperature=temperature, seed=seed)
    new = [msg]
    actions = []
    for tc in msg.get("tool_calls", []) or []:
        name = tc["function"]["name"]
        try:
            args = json.loads(tc["function"]["arguments"] or "{}")
        except json.JSONDecodeError:
            args, out = None, "invalid JSON arguments"
        if args is not None:
            out = ws.call(name, args)
        new.append({"role": "tool", "tool_call_id": tc["id"], "content": out})
        actions.append({"tool": name, "args": {k: (v if len(str(v)) < 200 else str(v)[:200] + "...") for k, v in (args or {}).items()},
                        "result": str(out)[:500]})
    if not msg.get("tool_calls"):
        # model answered in prose: nudge it to keep using tools or submit
        new.append({"role": "user", "content": "Continue using the tools, or call submit if the task is complete."})
    return new, {"actions": actions, "reasoning": (reasoning or "")[:2000], "text": (msg.get("content") or "")[:1000]}


def _final(ws):
    g = grade(ws.task_dir, ws.root)
    return {"solved": g["solved"], "reward": g["reward"],
            "steps": [{"check": s["check"], "ok": s["ok"], "score": s["score"]} for s in g["steps"]]}


# =============================================================================== linear
def run_linear(task_dir, client, max_calls=30, temperature=0.6, seed=None):
    ws = Workspace(task_dir)
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": _user_msg(ws.spec)}]
    trace, t0 = [], time.time()
    for i in range(max_calls):
        new, info = _act(client, ws, messages, temperature, seed=None if seed is None else seed + i)
        messages += new
        trace.append(info)
        if ws.submitted:
            break
    res = {"method": "linear", "calls": len(trace), "submitted": ws.submitted, "trace": trace,
           "final": _final(ws), "seconds": round(time.time() - t0, 1)}
    return res, ws, messages


# =============================================================================== best-of-N
def run_bestofn(task_dir, client, prm, n=3, max_calls=30):
    def one(k):
        r, ws, msgs = run_linear(task_dir, client, max_calls=max_calls, temperature=0.9, seed=1000 * k)
        return (r, ws, msgs)

    with ThreadPoolExecutor(max_workers=n) as ex:
        raw = list(ex.map(one, range(n)))
    if prm.jury is not None or prm.cjudge is not None:
        start = Workspace(task_dir)
        e0 = prm.exec_score(start)
        summ = lambda r: {"actions": [a for t in r["trace"][-6:] for a in t["actions"]]}
        vals = prm.value_batch(start, {**e0[1], "exec": e0[0]}, {}, [(ws, msgs, summ(r)) for r, ws, msgs in raw])
        start.cleanup()
    else:
        vals = [prm.value(ws, msgs) for _, ws, msgs in raw]
    runs = [(v, det, r, ws) for (v, det), (r, ws, _) in zip(vals, raw)]
    best = max(runs, key=lambda x: x[0])
    for _, _, _, w in runs:
        if w is not best[3]:
            w.cleanup()
    return {"method": "bestofn", "n": n, "calls": sum(r[2]["calls"] for r in runs),
            "prm_values": [round(r[0], 3) for r in runs], "chosen": runs.index(best),
            "any_solved": any(r[2]["final"]["solved"] for r in runs),
            "final": best[2]["final"], "runs": [{"calls": r[2]["calls"], "final": r[2]["final"], "prm": r[1]} for r in runs]}


# =============================================================================== MCTS
@dataclass
class Node:
    id: int
    ws: Workspace
    messages: list
    parent: "Node | None" = None
    children: list = field(default_factory=list)
    N: int = 0
    W: float = 0.0
    value: float = 0.0
    prm: dict = field(default_factory=dict)
    info: dict = field(default_factory=dict)
    depth: int = 0
    terminal: bool = False
    exhausted: bool = False

    @property
    def Q(self):
        return self.W if self.N else self.value


def run_mcts(task_dir, client, prm, max_calls=90, k=3, c_uct=0.5, max_depth=30, temperature=0.9, log=None,
             rollout=4, diag_top=5):
    """
    Selection: UCT.  Expansion: k sampled agent turns from the selected node (each child = forked
    workspace + that turn's tool results).  Evaluation: PRM value of the child (no random rollout,
    as in ReST-MCTS*).  Backup: mean.  A child that calls submit is terminal.
    Final answer: the node with the highest PRM value among submitted nodes (else among all nodes).
    """
    t0 = time.time()
    root_ws = Workspace(task_dir)
    root = Node(0, root_ws, [{"role": "system", "content": SYSTEM}, {"role": "user", "content": _user_msg(root_ws.spec)}])
    root.value, root.prm = prm.value(root_ws, root.messages) if (prm.jury is None and prm.cjudge is None) else (lambda e: (e[0], {**e[1], "exec": round(e[0], 4)}))(prm.exec_score(root_ws))
    nodes, calls = [root], 0
    rng = random.Random(0)

    def expandable(n):
        return not n.terminal and not n.exhausted and n.depth < max_depth

    while calls < max_calls:
        # ---- select (UCT over non-exhausted children)
        node = root
        while node.children:
            live = [c for c in node.children if not c.exhausted and (expandable(c) or c.children)]
            if not live:
                node.exhausted = True         # every branch below is finished
                break
            node = max(live, key=lambda c: c.Q + c_uct * math.sqrt(math.log(node.N + 1) / (1 + c.N)) + 1e-6 * rng.random())
        if not expandable(node):
            if root.exhausted or not any(expandable(n) and not n.children for n in nodes):
                break                         # nothing left to expand
            continue
        # ---- expand: k sampled turns in parallel (each in its own forked workspace)
        kk = min(k, max_calls - calls)
        seeds = [rng.randrange(1 << 30) for _ in range(kk)]

        def grow(seed, parent=node):
            ws = parent.ws.fork()
            new, info = _act(client, ws, parent.messages, temperature, seed=seed)
            return ws, new, info

        with ThreadPoolExecutor(max_workers=kk) as ex:
            grown = list(ex.map(grow, seeds))
        calls += kk
        vals = prm.value_batch(node.ws, node.prm, node.info, [(w, node.messages + n, i) for w, n, i in grown])
        for (ws, new, info), (v, det) in zip(grown, vals):
            child = Node(len(nodes), ws, node.messages + new, parent=node, depth=node.depth + 1, info=info,
                         terminal=ws.submitted, value=v, prm=det)
            node.children.append(child)
            nodes.append(child)
            _backup(child, child.value)
            if log:
                log(f"  node {child.id} d={child.depth} v={child.value:.3f} "
                    f"{[a['tool'] for a in info['actions']]} {child.prm}")

        # ---- rollout (LATS-style simulation): continue the best new child greedily for a few turns,
        #      adding each step to the tree so depth grows every iteration, not just breadth.
        cur = max(node.children[-kk:], key=lambda c: c.value) if kk else None
        for _ in range(rollout):
            if cur is None or not expandable(cur) or calls >= max_calls:
                break
            ws = cur.ws.fork()
            new, info = _act(client, ws, cur.messages, 0.6, seed=rng.randrange(1 << 30))
            calls += 1
            (v, det), = prm.value_batch(cur.ws, cur.prm, cur.info, [(ws, cur.messages + new, info)])
            nxt = Node(len(nodes), ws, cur.messages + new, parent=cur, depth=cur.depth + 1, info=info,
                       terminal=ws.submitted, value=v, prm=det)
            cur.children.append(nxt)
            nodes.append(nxt)
            _backup(nxt, nxt.value)
            if log:
                log(f"  node {nxt.id} d={nxt.depth} v={nxt.value:.3f} (rollout) {[a['tool'] for a in info['actions']]} {nxt.prm}")
            cur = nxt

    # ---- choose answer
    submitted = [n for n in nodes if n.terminal]
    pool = submitted or nodes
    ex = lambda n: n.prm.get("exec", n.value)
    best = max(pool, key=lambda n: (n.value, -n.depth)) if submitted else max(pool, key=lambda n: (ex(n), n.value))
    final = _final(best.ws)
    # diagnostics only (never used for selection): did the search FIND a solution the PRM failed to pick?
    diag = []
    seen = set()
    for n in sorted(nodes, key=lambda n: -n.value):
        dg = n.ws.digest()
        if dg in seen:
            continue
        seen.add(dg)
        g = _final(n.ws)
        diag.append({"node": n.id, "value": round(n.value, 4), "hidden_reward": g["reward"], "hidden_solved": g["solved"]})
        if len(diag) >= diag_top:
            break
    tree = [{"id": n.id, "parent": n.parent.id if n.parent else None, "depth": n.depth, "N": n.N,
             "Q": round(n.Q, 4), "value": round(n.value, 4), "prm": n.prm, "terminal": n.terminal,
             "actions": n.info.get("actions", []), "reasoning": n.info.get("reasoning", "")[:600]} for n in nodes]
    for n in nodes:
        if n is not best:
            n.ws.cleanup()
    return {"method": "mcts", "calls": calls, "nodes": len(nodes), "chosen_node": best.id,
            "chosen_from": "submitted" if submitted else "all", "final": final, "tree": tree,
            "diag_topk_hidden": diag, "any_topk_solved": any(x["hidden_solved"] for x in diag),
            "seconds": round(time.time() - t0, 1)}


def _backup(node, v):
    """Max-backup (deterministic environment): Q(n) = best value seen in n's subtree."""
    while node is not None:
        node.N += 1
        node.W = max(node.W, v) if node.N > 1 else v
        node = node.parent
