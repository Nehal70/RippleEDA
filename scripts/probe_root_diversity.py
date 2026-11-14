"""Measure how different Ripple's k root proposals are, with no tool execution and no tree traversal.

For every task under tasks/G_*/ this builds the SAME root prompt the live search sends
(core/openrouter_client.py RippleActor.propose + core/live_world.py LiveChipWorld._history, with
PROPOSE_INSTRUCTIONS as the system prompt and TOOL_MENU as the tool list), asks Ripple for k=3 actions
once at temperature 0.8, and for a random subset asks a second time to measure sample-to-sample variance.
Raw responses go to results/root_probe/<task>.json; a summary is printed at the end.

    python3 scripts/probe_root_diversity.py                  # run the probe (skips tasks already in results/)
    python3 scripts/probe_root_diversity.py --summarize      # only re-read results/ and print the summary
    python3 scripts/probe_root_diversity.py --force --mode forced   # redo everything with the forced tool call

Answer path (`--mode`):
  plain   exactly what the live search does: free-text completion, JSON extracted from the text.
  forced  forced tool call `propose_actions` with reasoning disabled (how core/calibrated_judge.py gets
          reliable answers; free text came back empty 56/64 times in the judge experiment).
  auto    plain first; if the text is empty or not JSON, fall back to forced and record both.  (default)
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.openrouter_client import API_URL, DEFAULT_MODEL, _json_object, load_env_file  # noqa: E402
from core.prompts import PROPOSE_INSTRUCTIONS  # noqa: E402
from core.search_types import Action  # noqa: E402
from core.tool_runner import TOOL_MENU  # noqa: E402
from core.verifier import BANNED_IN_SEARCH  # noqa: E402

K = 3
TEMPERATURE = 0.8            # RippleActor.propose uses 0.8
OUT_DIR = ROOT / "results" / "root_probe"
TASK_GLOB = "G_*"
PRICE_IN, PRICE_OUT = 0.10e-6, 0.30e-6   # fallback $/token (agent/llm.py) when OpenRouter omits usage.cost
ARG_KEYS_RUNNER_READS = {"filepath", "design"}   # core/tool_runner.execute only looks at these

PROPOSE_TOOL = [{"type": "function", "function": {
    "name": "propose_actions",
    "description": f"Submit exactly {K} different next tool calls.",
    "parameters": {"type": "object", "properties": {
        "actions": {"type": "array", "items": {"type": "object", "properties": {
            "tool": {"type": "string", "description": "a tool name from the list"},
            "args": {"type": "object", "description": "arguments, e.g. {\"filepath\": ...}"},
            "rationale": {"type": "string", "description": "short"}},
            "required": ["tool", "args", "rationale"]}}},
        "required": ["actions"]}}}]


# ----------------------------------------------------------------------------- prompt
def load_task(task_dir: Path) -> dict:
    """task.yaml -> dict. PyYAML if present, else a minimal parse of the `prompt: |` block."""
    text = (task_dir / "task.yaml").read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text)
    except Exception:
        m = re.search(r"^prompt:\s*\|\s*\n((?:[ \t]+.*\n?)+)", text, flags=re.M)
        prompt = "\n".join(l.strip() for l in m.group(1).splitlines()) if m else text
        rtl = re.search(r"^rtl:\s*\[(.*?)\]", text, flags=re.M)
        return {"prompt": prompt, "rtl": [s.strip() for s in rtl.group(1).split(",")] if rtl else []}


def build_root(task_dir: Path, include_contents: bool) -> dict:
    """Return {task, design_path, smoke_tb, history, system, user}. Mirrors LiveChipWorld._history +
    RippleActor.propose. Only start/ is visible: tasks/<id>/tb and solution are hidden from the agent."""
    meta = load_task(task_dir)
    start = task_dir / "start"
    rtl_rel = (meta.get("rtl") or ["rtl/design.sv"])[0]
    design = start / rtl_rel
    if not design.exists():
        cands = sorted((start / "rtl").glob("*.sv"))
        design = cands[0] if cands else design
    tbs = sorted((start / "tb").glob("tb_*smoke*.sv")) + sorted((start / "tb").glob("*.sv"))
    smoke = tbs[0] if tbs else None
    task_text = " ".join(str(meta.get("prompt", "")).split())
    lines = [f"Task: {task_text}", f"Design: {design}"]          # exactly what the live world sends
    if smoke is not None:
        lines.append(f"Smoke testbench: {smoke}")
    if include_contents:
        lines.append(f"--- {rtl_rel} ---")
        lines.append(design.read_text(encoding="utf-8", errors="replace"))
    history = "\n".join(lines)
    user = f"Give {K} different actions.\nTools:\n{TOOL_MENU}\n\nHistory:\n{history}"
    return {"task": task_text, "design_path": str(design), "smoke_tb": str(smoke) if smoke else "",
            "history": history, "system": PROPOSE_INSTRUCTIONS, "user": user}


# ----------------------------------------------------------------------------- transport
class Client:
    def __init__(self, model: str, timeout: int):
        load_env_file()
        self.key = os.environ.get("OPENROUTER_API_KEY", "")
        if not self.key or self.key.startswith("your_"):
            raise RuntimeError("OPENROUTER_API_KEY not set in .env")
        self.model = model
        self.timeout = timeout
        self.lock = threading.Lock()
        self.calls = 0
        self.usd = 0.0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def _post(self, body: dict) -> tuple[dict | None, str, float]:
        """Returns (payload, error, latency_s). Retries 429/5xx/timeouts a few times."""
        delay = 3.0
        err = ""
        t0 = time.time()
        for attempt in range(4):
            req = urllib.request.Request(API_URL, data=json.dumps(body).encode("utf-8"), method="POST",
                                         headers={"Authorization": f"Bearer {self.key}",
                                                  "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    payload = json.loads(r.read().decode("utf-8"))
                usage = payload.get("usage") or {}
                with self.lock:
                    self.calls += 1
                    cost = usage.get("cost")
                    if cost is None:
                        cost = (usage.get("prompt_tokens") or 0) * PRICE_IN + (usage.get("completion_tokens") or 0) * PRICE_OUT
                    self.usd += float(cost or 0.0)
                    self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
                    self.completion_tokens += int(usage.get("completion_tokens") or 0)
                return payload, "", time.time() - t0
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", errors="replace")[:300]
                err = f"HTTP {e.code}: {detail}"
                if e.code in (400, 401, 402, 404):
                    break
                if e.code == 429:
                    delay = max(delay, 10.0)
            except Exception as e:  # timeouts, connection resets
                err = f"{type(e).__name__}: {e}"
            time.sleep(delay + random.random() * 2)
            delay = min(delay * 2, 60)
        return None, err, time.time() - t0

    def propose_plain(self, system: str, user: str) -> dict:
        """Same request body as OpenRouterClient.complete (plus usage accounting)."""
        body = {"model": self.model, "temperature": TEMPERATURE,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "usage": {"include": True}}
        payload, err, dt = self._post(body)
        rec = {"path": "plain", "latency_s": round(dt, 2), "error": err, "content": "", "reasoning_chars": 0,
               "finish_reason": "", "usage": {}}
        if payload is None:
            return rec
        ch = (payload.get("choices") or [{}])[0]
        msg = ch.get("message") or {}
        rec.update(content=msg.get("content") or "", reasoning_chars=len(msg.get("reasoning") or ""),
                   finish_reason=ch.get("finish_reason") or ch.get("native_finish_reason") or "",
                   usage=payload.get("usage") or {}, raw_message=msg)
        return rec

    def propose_forced(self, system: str, user: str) -> dict:
        """Forced tool call with reasoning off: how calibrated_judge._chat gets reliable answers."""
        body = {"model": self.model, "temperature": TEMPERATURE, "max_tokens": 4000,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "tools": PROPOSE_TOOL, "tool_choice": {"type": "function", "function": {"name": "propose_actions"}},
                "reasoning": {"enabled": False}, "usage": {"include": True}}
        payload, err, dt = self._post(body)
        rec = {"path": "forced", "latency_s": round(dt, 2), "error": err, "content": "", "reasoning_chars": 0,
               "finish_reason": "", "usage": {}}
        if payload is None:
            return rec
        ch = (payload.get("choices") or [{}])[0]
        msg = ch.get("message") or {}
        tcs = msg.get("tool_calls") or []
        rec.update(content=(tcs[0]["function"].get("arguments") if tcs else "") or "",
                   text_content=msg.get("content") or "", reasoning_chars=len(msg.get("reasoning") or ""),
                   finish_reason=ch.get("finish_reason") or ch.get("native_finish_reason") or "",
                   usage=payload.get("usage") or {}, raw_message=msg)
        return rec


# ----------------------------------------------------------------------------- parsing / classification
def parse_actions(content: str) -> tuple[list[dict], str]:
    """-> (actions, failure). failure in {'', 'empty', 'unparseable', 'no_actions'}."""
    if not (content or "").strip():
        return [], "empty"
    try:
        data = _json_object(content)
    except Exception:
        return [], "unparseable"
    items = data.get("actions") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        return [], "no_actions"
    out = []
    for it in items[:K]:
        if not isinstance(it, dict):
            continue
        args = it.get("args")
        out.append({"tool": str(it.get("tool", "")), "args": args if isinstance(args, dict) else {"_raw": args},
                    "rationale": str(it.get("rationale", ""))})
    return out, ""


def path_ok(p: str, task_dir: Path) -> bool:
    pp = Path(str(p))
    return pp.exists() or (task_dir / "start" / pp).exists() or (task_dir / pp).exists() or (ROOT / pp).exists()


def _pathlike(v) -> list[str]:
    vals = v if isinstance(v, list) else [v]
    return [str(x) for x in vals if isinstance(x, str) and re.search(r"\.(sv|v|vh|svh)$|/", x)]


def classify(action: dict, task_dir: Path) -> dict:
    """bad_args: args not a dict, or a path-like argument (any key) that does not resolve.
    runner_ignores_args: args given but none under the keys core/tool_runner.execute reads."""
    tool, args = action["tool"], action["args"]
    flags = {"banned": tool in BANNED_IN_SEARCH, "nonexistent": tool not in TOOL_MENU, "bad_args": False,
             "unknown_arg_keys": sorted(k for k in args if k not in ARG_KEYS_RUNNER_READS),
             "runner_ignores_args": bool(args) and not (set(args) & ARG_KEYS_RUNNER_READS)}
    if "_raw" in args:
        flags["bad_args"] = True
    for key, val in args.items():
        for p in _pathlike(val):
            if not path_ok(p, task_dir):
                flags["bad_args"] = True
                flags.setdefault("bad_paths", []).append(f"{key}={p}")
    return flags


def action_key(a: dict) -> str:
    return Action(tool=a["tool"], args=a["args"], rationale="").key()


def exec_key(a: dict, design_path: str) -> str:
    """What core/tool_runner.execute would actually run: tool family + the one path it reads."""
    tool, args = a["tool"], a["args"]
    if tool in BANNED_IN_SEARCH:
        fam = "banned"
    elif tool in {"verible_lint", "verilator_compile", "verilator_simulate"}:
        fam = "verilator_lint"
    elif tool in {"yosys_analyze", "yosys_synth", "yosys_show"}:
        fam = "yosys_synth"
    else:
        fam = "not_executable"
    path = str(args.get("filepath") or args.get("design") or design_path)
    return f"{fam}|{path}"


# ----------------------------------------------------------------------------- one call
def one_call(client: Client, mode: str, root: dict, task_dir: Path, attempt: int) -> dict:
    rec = {"attempt": attempt, "mode": mode, "tries": [], "used": "", "failure": "", "actions": []}
    order = {"plain": ["plain"], "forced": ["forced"], "auto": ["plain", "forced"]}[mode]
    for path in order:
        r = client.propose_plain(root["system"], root["user"]) if path == "plain" \
            else client.propose_forced(root["system"], root["user"])
        actions, failure = parse_actions(r["content"]) if not r["error"] else ([], "http_error")
        r["parse_failure"] = failure
        rec["tries"].append(r)
        if not failure:
            rec["used"], rec["actions"], rec["failure"] = path, actions, ""
            break
        rec["failure"] = failure
    rec["flags"] = [classify(a, task_dir) for a in rec["actions"]]
    rec["keys"] = [action_key(a) for a in rec["actions"]]
    rec["latency_s"] = round(sum(t["latency_s"] for t in rec["tries"]), 2)
    rec["usd"] = round(sum(float((t.get("usage") or {}).get("cost") or 0.0) for t in rec["tries"]), 6)
    return rec


# ----------------------------------------------------------------------------- summary
def summarize(records: dict[str, dict], examples: int = 5) -> None:
    tasks = sorted(records)
    for t in tasks:
        for c in records[t].get("calls") or []:
            c["_task"] = t
    first = {t: records[t]["calls"][0] for t in tasks if records[t].get("calls")}
    repeats = {t: records[t]["calls"][1] for t in tasks if len(records[t].get("calls") or []) > 1}
    all_calls = [c for t in tasks for c in records[t].get("calls") or []]

    print("\n" + "=" * 78)
    print(f"ROOT PROPOSAL DIVERSITY  k={K} temp={TEMPERATURE}  tasks={len(tasks)}  calls={len(all_calls)}")
    print("=" * 78)
    print("\nPer task (first call): unique tools / unique (tool,args) keys / tools proposed")
    for t in tasks:
        c = first.get(t)
        if c is None:
            continue
        tools = [a["tool"] for a in c["actions"]]
        if c["failure"]:
            print(f"  {t:12} FAIL={c['failure']:<12} via {','.join(x['path'] for x in c['tries'])}")
            continue
        print(f"  {t:12} tools={len(set(tools))} keys={len(set(c['keys']))}  {tools}  [{c['used']}]")

    ok = [c for c in all_calls if not c["failure"]]
    n = len(all_calls)
    print(f"\nCalls: {n}  ok={len(ok)}  failed={n - len(ok)}")
    fail = Counter(c["failure"] for c in all_calls if c["failure"])
    for f, cnt in fail.most_common():
        print(f"  failure {f}: {cnt}/{n} ({cnt / max(1, n):.0%})")
    used = Counter(c["used"] for c in ok)
    print(f"  answer path that succeeded: {dict(used)}")
    plain_tries = [t for c in all_calls for t in c["tries"] if t["path"] == "plain"]
    if plain_tries:
        pe = sum(1 for t in plain_tries if t["parse_failure"] == "empty")
        pu = sum(1 for t in plain_tries if t["parse_failure"] == "unparseable")
        ph = sum(1 for t in plain_tries if t["parse_failure"] == "http_error")
        fr = Counter(t.get("finish_reason", "") for t in plain_tries)
        print(f"  plain (live) path: {len(plain_tries)} tries, empty={pe}, unparseable={pu}, http_error={ph}, "
              f"ok={len(plain_tries) - pe - pu - ph}; finish_reason={dict(fr)}")
        rc = [t["reasoning_chars"] for t in plain_tries]
        if any(rc):
            print(f"  plain path returned hidden reasoning in {sum(1 for x in rc if x)} tries (mean {sum(rc) / len(rc):.0f} chars)")

    dist = Counter(len(set(c["keys"])) for c in ok)
    dist_tools = Counter(len(set(a["tool"] for a in c["actions"])) for c in ok)
    ncount = Counter(len(c["actions"]) for c in ok)
    print(f"\nDistinct (tool,args) candidates out of {K} [Action.key()]: "
          + ", ".join(f"{d}->{dist[d]} ({dist[d] / max(1, len(ok)):.0%})" for d in sorted(dist)))
    print(f"Distinct tool NAMES out of {K}: "
          + ", ".join(f"{d}->{dist_tools[d]} ({dist_tools[d] / max(1, len(ok)):.0%})" for d in sorted(dist_tools)))
    print(f"Number of actions returned: {dict(sorted(ncount.items()))}")
    dist_exec = Counter(len(set(exec_key(a, records[c["_task"]]["design_path"]) for a in c["actions"])) for c in ok)
    print(f"Distinct AS-EXECUTED by core/tool_runner (tool family + path it reads) out of {K}: "
          + ", ".join(f"{d}->{dist_exec[d]} ({dist_exec[d] / max(1, len(ok)):.0%})" for d in sorted(dist_exec)))

    firsts = Counter(c["actions"][0]["tool"] for c in ok if c["actions"])
    print("\nMost common FIRST-choice tools: " + ", ".join(f"{t}={k}" for t, k in firsts.most_common()))
    alltools = Counter(a["tool"] for c in ok for a in c["actions"])
    print("All proposed tools:            " + ", ".join(f"{t}={k}" for t, k in alltools.most_common()))
    sets = Counter(tuple(sorted(set(a["tool"] for a in c["actions"]))) for c in ok)
    print("Most common tool SETS:          " + "; ".join(f"{'+'.join(s)}={k}" for s, k in sets.most_common(6)))

    acts = [(a, f) for c in ok for a, f in zip(c["actions"], c["flags"])]
    nb = sum(1 for _, f in acts if f["banned"])
    nn = sum(1 for _, f in acts if f["nonexistent"] and not f["banned"])
    nba = sum(1 for _, f in acts if f["bad_args"])
    cb = sum(1 for c in ok if any(f["banned"] for f in c["flags"]))
    cn = sum(1 for c in ok if any(f["nonexistent"] and not f["banned"] for f in c["flags"]))
    cba = sum(1 for c in ok if any(f["bad_args"] for f in c["flags"]))
    nig = sum(1 for _, f in acts if f.get("runner_ignores_args"))
    print(f"\nActions: {len(acts)} total; banned={nb}, nonexistent tool={nn}, bad args (path does not resolve)={nba}")
    print(f"Calls with >=1 banned={cb}/{len(ok)} ({cb / max(1, len(ok)):.0%}), nonexistent={cn}/{len(ok)} "
          f"({cn / max(1, len(ok)):.0%}), bad args={cba}/{len(ok)} ({cba / max(1, len(ok)):.0%})")
    argkeys = Counter(k for a, _ in acts for k in a["args"])
    print("Arg keys used:                  " + ", ".join(f"{k}={v}" for k, v in argkeys.most_common(12)))
    noargs = sum(1 for a, _ in acts if not a["args"])
    print(f"Actions with EMPTY args: {noargs}/{len(acts)}; actions whose args the runner IGNORES (no "
          f"filepath/design key): {nig}/{len(acts)} -> both run on the task design path")
    bad = Counter(p.split("=")[0] for _, f in acts for p in f.get("bad_paths", []))
    if bad:
        print("Unresolvable path args by key:  " + ", ".join(f"{k}={v}" for k, v in bad.most_common()))

    lat = [c["latency_s"] for c in all_calls]
    usd = sum(c["usd"] for c in all_calls)
    ptok = sum(int((t.get("usage") or {}).get("prompt_tokens") or 0) for c in all_calls for t in c["tries"])
    ctok = sum(int((t.get("usage") or {}).get("completion_tokens") or 0) for c in all_calls for t in c["tries"])
    est = ptok * PRICE_IN + ctok * PRICE_OUT
    print(f"\nLatency: mean {sum(lat) / max(1, len(lat)):.1f}s  max {max(lat) if lat else 0:.1f}s per call (incl. fallback)")
    print(f"Cost: ${usd:.4f} from OpenRouter usage.cost" + (f" (est ${est:.4f} from {ptok}+{ctok} tokens)" if not usd else
                                                            f"; tokens {ptok} in / {ctok} out"))

    if repeats:
        same_keys = same_tools = same_exec = both_ok = 0
        overlap = []
        for t, c2 in repeats.items():
            c1 = first[t]
            if c1["failure"] or c2["failure"]:
                continue
            both_ok += 1
            k1, k2 = set(c1["keys"]), set(c2["keys"])
            s1, s2 = set(a["tool"] for a in c1["actions"]), set(a["tool"] for a in c2["actions"])
            dp = records[t]["design_path"]
            e1, e2 = set(exec_key(a, dp) for a in c1["actions"]), set(exec_key(a, dp) for a in c2["actions"])
            same_keys += k1 == k2
            same_tools += s1 == s2
            same_exec += e1 == e2
            overlap.append(len(k1 & k2) / max(1, len(k1 | k2)))
        print(f"\nRepeat calls on {len(repeats)} tasks ({both_ok} with both calls ok):")
        if both_ok:
            print(f"  candidate SET (tool,args) differed between call 1 and 2: {both_ok - same_keys}/{both_ok} "
                  f"({(both_ok - same_keys) / both_ok:.0%}); tool-name set differed: {both_ok - same_tools}/{both_ok}; "
                  f"as-executed set differed: {both_ok - same_exec}/{both_ok}")
            print(f"  mean Jaccard overlap of key sets: {sum(overlap) / len(overlap):.2f}")
            for t, c2 in sorted(repeats.items()):
                c1 = first[t]
                if c1["failure"] or c2["failure"]:
                    continue
                print(f"    {t:12} 1st={[a['tool'] for a in c1['actions']]}  2nd={[a['tool'] for a in c2['actions']]}"
                      f"  {'SAME' if set(c1['keys']) == set(c2['keys']) else 'DIFF'}")

    # ---- verbatim examples: one task per tool SET, most common set first, then rarer ones, over
    #      different design families where possible, so the reader sees the typical case and the outliers.
    print("\n" + "-" * 78)
    print(f"{examples} verbatim examples (first call; rationale truncated)")
    print("-" * 78)
    set_of = {t: tuple(sorted(set(a["tool"] for a in first[t]["actions"]))) for t in first if not first[t]["failure"]}
    set_counts = Counter(set_of.values())
    chosen, fam_seen = [], set()
    for s, _ in set_counts.most_common():
        for t in sorted(set_of, key=lambda x: (x.split("_")[1] in fam_seen, x)):
            if set_of[t] == s and t not in chosen:
                chosen.append(t)
                fam_seen.add(t.split("_")[1])
                break
        if len(chosen) >= examples:
            break
    for t in tasks:
        if len(chosen) >= examples:
            break
        if t not in chosen and first.get(t) and not first[t]["failure"]:
            chosen.append(t)
    for t in chosen:
        c = first[t]
        n_exec = len(set(exec_key(a, records[t]["design_path"]) for a in c["actions"]))
        print(f"\n[{t}]  distinct keys={len(set(c['keys']))}/{len(c['actions'])}  as-executed={n_exec}  via {c['used']}"
              f"  (this tool set seen in {set_counts[set_of[t]]} first calls)\n  task: {records[t]['task'][:110]}...")
        for i, (a, f) in enumerate(zip(c["actions"], c["flags"]), 1):
            tag = " ".join(x for x, y in (("BANNED", f["banned"]), ("NONEXISTENT", f["nonexistent"] and not f["banned"]),
                                          ("BAD_ARGS", f["bad_args"])) if y)
            print(f"  {i}. {a['tool']}  args={json.dumps(a['args'])}  {('<' + tag + '>') if tag else ''}")
            print(f"     rationale: {a['rationale'][:160]}")


# ----------------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["plain", "forced", "auto"], default="auto")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=180, help="seconds per HTTP call")
    ap.add_argument("--repeat", type=int, default=10, help="how many random tasks get a second call")
    ap.add_argument("--seed", type=int, default=0, help="seed for the repeat subset")
    ap.add_argument("--limit", type=int, default=0, help="only the first N tasks (smoke test)")
    ap.add_argument("--tasks", nargs="*", help="explicit task ids")
    ap.add_argument("--no-contents", action="store_true",
                    help="do not append the RTL file contents to the history (bare live-world prompt)")
    ap.add_argument("--force", action="store_true", help="re-query tasks that already have a result file")
    ap.add_argument("--summarize", action="store_true", help="no API calls; summarize results/root_probe")
    ap.add_argument("--model", default=os.environ.get("RIPPLE_MODEL") or DEFAULT_MODEL)
    args = ap.parse_args()

    task_dirs = sorted(p for p in (ROOT / "tasks").glob(TASK_GLOB) if (p / "task.yaml").exists())
    if args.tasks:
        task_dirs = [p for p in task_dirs if p.name in set(args.tasks)]
    if args.limit:
        task_dirs = task_dirs[: args.limit]
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.summarize:
        recs = {}
        for f in sorted(OUT_DIR.glob("*.json")):
            d = json.loads(f.read_text(encoding="utf-8"))
            if not task_dirs or f.stem in {p.name for p in task_dirs}:
                recs[f.stem] = d
        if not recs:
            print(f"no results in {OUT_DIR}")
            return
        summarize(recs)
        return

    model = args.model if not args.model.startswith("your_") else DEFAULT_MODEL
    client = Client(model, args.timeout)
    rng = random.Random(args.seed)
    repeat_set = set(rng.sample([p.name for p in task_dirs], min(args.repeat, len(task_dirs))))
    print(f"model={model} mode={args.mode} tasks={len(task_dirs)} repeat={sorted(repeat_set)} "
          f"threads={args.threads} timeout={args.timeout}s contents={'no' if args.no_contents else 'yes'}")

    def work(task_dir: Path) -> tuple[str, dict]:
        out = OUT_DIR / f"{task_dir.name}.json"
        if out.exists() and not args.force:
            return task_dir.name, json.loads(out.read_text(encoding="utf-8"))
        root = build_root(task_dir, include_contents=not args.no_contents)
        n_calls = 2 if task_dir.name in repeat_set else 1
        calls = [one_call(client, args.mode, root, task_dir, i) for i in range(n_calls)]
        rec = {"task_id": task_dir.name, "model": model, "mode": args.mode, "k": K, "temperature": TEMPERATURE,
               "task": root["task"], "design_path": root["design_path"], "smoke_tb": root["smoke_tb"],
               "prompt": {"system": root["system"], "user": root["user"]}, "calls": calls}
        out.write_text(json.dumps(rec, indent=2), encoding="utf-8")
        return task_dir.name, rec

    records: dict[str, dict] = {}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.threads) as ex:
        futs = {ex.submit(work, p): p.name for p in task_dirs}
        for i, fut in enumerate(as_completed(futs), 1):
            name = futs[fut]
            try:
                _, rec = fut.result()
                records[name] = rec
                c0 = rec["calls"][0]
                tools = [a["tool"] for a in c0["actions"]]
                status = f"FAIL({c0['failure']})" if c0["failure"] else f"ok[{c0['used']}] keys={len(set(c0['keys']))}"
                print(f"[{i:2}/{len(task_dirs)}] {name:12} {status:22} {tools}  {c0['latency_s']:.0f}s"
                      f"{'  (+repeat)' if len(rec['calls']) > 1 else ''}  elapsed {time.time() - t0:.0f}s", flush=True)
            except Exception as e:
                print(f"[{i:2}/{len(task_dirs)}] {name:12} ERROR {type(e).__name__}: {e}", flush=True)
    print(f"\nHTTP calls this run: {client.calls}, cost ${client.usd:.4f} "
          f"({client.prompt_tokens} in / {client.completion_tokens} out tokens)")
    summarize(records)


if __name__ == "__main__":
    main()
