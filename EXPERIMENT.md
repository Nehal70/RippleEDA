# RippleEDA: what we built, why, and how we are measuring it

Context for anyone writing up or visualizing the results. Everything referenced here is in this repo
except the live run outputs, which get pushed under `results/` as they finish.

## 1. The claim

Ripple-65B is strong at executing single terminal steps but weaker at *routing* across a multi-step
tool chain (model card: 71.7% on MCP Atlas vs 89.2% for the best open model). In chip design the
expensive part is exactly that loop: which check to run next, how to read a failing sim or timing
report, which of several plausible fixes to apply. A linear agent commits to one reading of each tool
result and never revisits it.

We built an **inference-time search harness** around Ripple. No fine-tuning, no extra model. Ripple
proposes actions, real EDA tools decide which branches live, and a literature-grounded rubric ranks
the finished designs on correctness first, then PPA.

## 2. Evidence that the problem is real (before the harness)

### 2a. Linear Ripple baseline on the 50 generated tasks
There are two copies of the 50 generated multi-bug RTL repair tasks (afifo, alu, div, fifo, irq,
mac, spi, uart; 2–6 injected bugs each, drawn from a verified bug catalog):

| set | where | prompt | public tb | used for |
|---|---|---|---|---|
| original | `tasks_v1/` (local only, gitignored) | full spec in the prompt | yes (`tb/*_smoke.sv`) | **this experiment** |
| hardened | `tasks/` (committed, `1b4bb18`) | one line, spec only in the RTL header | none | follow-up, not run yet |

Both are graded by the same hidden signoff suite (`eda/grade.py`): lint, hidden testbench sim,
synthesis with no latches, STA at the task's clock period, cell budget. Hardened adds a 1.1x cell cap.

Linear Ripple on the **original** set (ReAct loop, 30 tool calls, one seed,
`scripts/run_linear_baseline.py`, `LINEAR_BASELINE.md`): **26/50 solved, 24 failed**. The 24
failures are `results/linear_failed.txt`. Everything below compares against this run.
The hardened set exists because 26/50 with a full spec and a public smoke test is already an easy
setting; we did not have time to re-baseline on it tonight.

### 2b. Root-diversity probe
`scripts/probe_root_diversity.py` asks Ripple for 3 candidate first moves on every task, no execution.
Results in `results/root_probe/` and `results/root_probe_summary.txt`:
- first-choice tool was `verible_lint` on 58/60 calls
- 46/60 calls proposed the identical triple `verible_lint → verilator_compile → verilator_simulate`
- once you account for what the tool runner actually executes, 75% of the triples collapse to a
  single distinct action
- (the probe used the mcp4eda tool names; the harness uses the 8 workspace tools below. Same model
  behaviour, different tool surface.)

So branching at the *root* is near-useless; the disagreement that matters happens after diagnostics,
at the diagnose/repair step. The harness is designed around that.

## 3. The harness

Files: `core/mcts_engine.py` (search), `core/task_world.py` (engine ↔ real tools), `core/verifier.py`
(scoring), `core/calibrated_judge.py` (judge prior), `agent/workspace.py` (isolated forked workspaces).

### Search loop (MCTS over tool calls)
Per round, up to 8 rounds, k = 3 candidates:
1. **Select** a node by UCB1. The exploit term is `blended_mean()`: the judge's prior guess until
   rollouts exist, then rollouts take over (`λ = visits / (visits + 1)`).
2. **Probe**: Ripple proposes 3 next actions (forced tool call, reasoning off; this is what keeps
   latency at 3–15 s and schema compliance at 100%). Each candidate is executed once on its own forked
   workspace, so branches cannot contaminate each other.
3. **Hard kill** from tool evidence: compile error, banned tool, latch when forbidden, cell cap
   blown. Also a **claim-vs-truth kill**: Ripple submits a structured claim about what it observed
   (error count, tests passed, cells…); if it disagrees with the tool's actual output the branch dies.
   This is the "did the model read the log correctly" check.
4. **Judge prior** for survivors: `CalibratedJudge`, two self-consistency votes, quotes must appear
   verbatim in the tool stdout, logistic map fitted to past rollout outcomes.
5. **Rollout**: up to 4 greedy steps from the node on a fresh fork, then `terminal_score` on the
   resulting design. Backed up to all ancestors.
6. Final answer = best submitted node by blended value; its workspace is what the hidden grader sees.

Worker tools: `list_files, read_file, write_file, run_lint, run_sim, run_synth, run_sta, submit`.
No shell, no access to `tb/` hidden tests.

### Scoring rubric (`core/verifier.py`)
Tiers, so a broken design can never outrank a correct one:
- crash / banned tool → 0.05
- partial correctness `c = passed/total` → `0.10 + 0.40·c` (ceiling 0.50)
- full pass → `0.60 + 0.40·PPA`
- PPA = weighted area 0.5, cycles 0.3, delay 0.2, each normalized against the *start design*:
  `score = clamp(0.5 + 0.5·(1 − metric/baseline))`. Match baseline → 0.5, halve it → 0.75.
- step penalty 0.02 per step, floored at the tier floor, so shorter clean trajectories win ties.
Sources: DeLorenzo et al. (MCTS for Verilog with synthesis feedback), RTLLM, VerilogEval, EvolVE.
If the start design already misses timing, `max_delay` is demoted from hard kill to a PPA term
(otherwise every branch that runs STA dies at the root).

## 4. The experiment

### Tasks (`results/exp_tasks.txt`, 30 tasks)
- 24 = every task linear Ripple failed
- 6 = controls linear solved, to check the harness does not break easy tasks

### Arms
| arm | code | budget | seeds |
|---|---|---|---|
| Linear, single run | `scripts/run_linear_baseline.py` | 30 calls | seed 0 (done, 26/50) |
| Linear, best-of-3 | same, `--seed 1`, `--seed 2` on the 24 failures | 3 × 30 = 90 calls | take max over seeds |
| Tree (ours) | `scripts/run_core_mcts.py --rounds 8 --k 3 --rollout-steps 4` | ≤ ~90 calls | one run |

Best-of-3 exists so nobody can say "linear would have solved it with the same 90 calls". Both arms
use the same model, same tool surface, same hidden grader, same tasks. Tree runs on a separate
OpenRouter account (`scripts/harness_env.sh`) only to split billing; the upstream rate limit is
shared regardless.

### Commands
```bash
# linear best-of-3 control (24 failures)
source env_mcts.sh
python3 scripts/run_linear_baseline.py --task-root tasks_v1 --tasks-file results/linear_failed.txt --workers 6 --max-calls 30 --seed 1 --out results/bo3_seed1
python3 scripts/run_linear_baseline.py --task-root tasks_v1 --tasks-file results/linear_failed.txt --workers 6 --max-calls 30 --seed 2 --out results/bo3_seed2

# tree
source scripts/harness_env.sh
python3 scripts/run_core_mcts.py --task-root tasks_v1 --tasks-file results/exp_tasks.txt \
  --workers 12 --rounds 8 --k 3 --rollout-steps 4 --out results/core_mcts_exp

# comparison
python3 scripts/compare_arms.py results/core_mcts_exp
```

### What we report
1. **Solve rate** on the 24 failures: linear seed 0 (0 by construction), linear best-of-3, tree.
2. **Controls**: tree solve rate on the 6 easy tasks (should be 6/6).
3. **Hidden reward** per task (0–1, continuous; partial credit on sim checks and timing), and the
   per-task delta tree − linear.
4. **PPA on tasks both arms solve**: the hidden grader gives 1.0 to any passing design, so "better
   than pass" is measured by re-synthesizing both final designs with the same yosys/STA flow and
   comparing cells and slack. Tree outputs store `final_files` and `grade_metrics` for this; linear
   outputs store `final_files`.
5. **Cost**: calls and USD per task, both arms.
6. **Leak check**: `compare_arms.py` scans every `run_shell` for `tb/`, `solution`, `/tmp/ws_`,
   `find /`; any hit is listed and that task is excluded. (One early unsandboxed linear run was
   caught doing exactly this; it is kept in `results/linear_baseline_v2_LEAKED_unsandboxed` and not
   used.)

### Output files
- `results/core_mcts_exp/<task>.json`: solved, reward, reward_start, chosen path, full tree
  (every node's tool, args, status, kill reason, judge prior, visits, blended value, measured truth),
  engine trace, final RTL, grader metrics, calls/usd. `summary.txt` is the one-line-per-task table.
- `results/bo3_seed{1,2}/<task>.json`: same shape as the seed-0 baseline, `summary.txt` table.
- `results/root_probe/`: probe outputs behind section 2b.

## 5. Known limitations, state them rather than hide them
- Upstream rate limiting on `ricursiveai/ripple-65b-preview` is the wall-clock bottleneck; the tree
  retries 429s for up to ~5 min per call instead of dropping the branch. A rollout that still gets no
  response scores 0.05 (crash tier), which drags that branch down for a non-design reason. We count
  how often this happened.
- Root branching is near-degenerate (section 2b); the value of the tree comes from branching at
  repair steps and from killing branches whose claimed observation disagrees with the tool.
- Tree uses ~3x the calls of a single linear run. Best-of-3 is the call-matched control.
- The judge is the same model as the worker. The blend is deliberately rollout-dominated so a
  self-flattering judge cannot carry a branch on its own.
- `max_delay` is only a hard kill when the start design meets timing.
