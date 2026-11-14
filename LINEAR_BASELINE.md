# Running the linear baseline

Plain linear Ripple (`agent.agents.run_linear`, no tree search) on the 50 hardened repair tasks under `tasks/G_*/`.
Each task is one buggy RTL file with the spec in its header comment and a one-line prompt. There is no public
testbench; the agent has to write its own. The hidden signoff suite in `eda/grade.py` scores the final design.

Everything below runs inside a WSL Ubuntu (24.04) shell from the repository root
(`cd /mnt/c/.../RippleEDA` on a Windows checkout). Commands are copy-pasteable.

### 1. EDA tools

The harness needs `yosys` (>= 0.36: the synth flow uses `dfflibmap -dont_use`), `iverilog`/`vvp`, `verilator`,
`sby` + `bitwuzla` (formal / equivalence tools the agent may call) and OpenSTA `sta`, plus the sky130hd liberty
file. If `source env_mcts.sh && which yosys iverilog verilator sby sta` already resolves everything and
`$SKY130_LIB` exists, skip to step 2. Otherwise:

```bash
# yosys / iverilog / verilator / sby / bitwuzla: OSS CAD Suite, unpacked where env_mcts.sh looks for it
mkdir -p ~/rippleeda/tools ~/rippleeda/pdk/sky130hd
cd ~/rippleeda/tools
curl -fL -o /tmp/oss-cad-suite.tgz \
  https://github.com/YosysHQ/oss-cad-suite-build/releases/download/2026-11-12/oss-cad-suite-linux-x64-20261112.tgz
tar -xzf /tmp/oss-cad-suite.tgz && rm /tmp/oss-cad-suite.tgz      # -> ~/rippleeda/tools/oss-cad-suite/bin
```

OpenSTA is not part of OSS CAD Suite. The simplest source is the OpenLane Docker image, which also carries the
sky130 PDK (this is the same image/volume `SETUP.md` steps 1 and 6 install: `sudo apt install docker.io`,
`docker pull efabless/openlane:latest`, `docker volume create ripple-eda-pdks` + the `volare enable` line).
With that in place:

```bash
# sky130hd liberty (the grader's default SKY130_LIB path)
docker run --rm -v ripple-eda-pdks:/root/.volare --entrypoint cat efabless/openlane:latest \
  /root/.volare/sky130A/libs.ref/sky130_fd_sc_hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib \
  > ~/rippleeda/pdk/sky130hd/sky130_fd_sc_hd__tt_025C_1v80.lib

# `sta` shim: runs OpenSTA from the image with /tmp, /mnt/c and $HOME mounted at identical paths
mkdir -p ~/rippleeda/tools/bin
cat > ~/rippleeda/tools/bin/sta <<'EOF'
#!/bin/bash
exec docker run --rm -i -v /tmp:/tmp -v /mnt/c:/mnt/c -v "$HOME:$HOME" -w "$PWD" \
  --user "$(id -u):$(id -g)" --entrypoint sta efabless/openlane:latest "$@"
EOF
chmod +x ~/rippleeda/tools/bin/sta
```

If you have a native OpenSTA build instead, just make sure `sta` is on `PATH` (e.g. symlink it into
`~/rippleeda/tools/bin/`) and point `SKY130_LIB` at your liberty file.

### 2. Environment

`env_mcts.sh` sets `RIPPLEEDA_HOME`, `PYTHONPATH`, puts `$EDA_TOOLS_ROOT/bin` and `$EDA_TOOLS_ROOT/oss-cad-suite/bin`
on `PATH`, and exports `OPENROUTER_API_KEY` from `.env`. Defaults: `EDA_TOOLS_ROOT=$HOME/rippleeda/tools`,
`SKY130_LIB=$HOME/rippleeda/pdk/sky130hd/sky130_fd_sc_hd__tt_025C_1v80.lib` (override either before sourcing if
your layout differs, e.g. `export EDA_TOOLS_ROOT=/opt/eda`).

```bash
cd /path/to/RippleEDA
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt     # Linux venv in the repo (required:
                                                                        # OSS CAD Suite ships its own python3
                                                                        # without openai/pyyaml, and env_mcts.sh
                                                                        # puts .venv/bin ahead of it on PATH)
cp -n .env.example .env && nano .env        # set OPENROUTER_API_KEY=sk-or-...   (never commit .env)
source env_mcts.sh                          # prints "[mcts] harness ready (key set)"
which yosys iverilog verilator sby sta && ls "$SKY130_LIB"
```

### 3. Regenerate and validate the tasks

The 50 tasks are deterministic (seed 0, recorded n/k per design); regenerating rewrites `tasks/G_*/` with the
same bug combinations:

```bash
python3 tasks/_gen/generate.py --all              # all 8 designs -> tasks/G_*  (adds max_cells from a reference synth)
python3 tasks/_gen/generate.py --verify fifo      # one catalog: every bug caught alone, header/pattern check
python3 tasks/_gen/generate.py --make fifo --n 7 --k 5   # one design only
python3 validate_tasks.py                         # every start/ must FAIL and every solution/ must PASS the hidden grader
python3 validate_tasks.py G_fifo G_alu_01         # subset by prefix
```

`validate_tasks.py` ends with `ALL TASKS VALID`; anything else lists the task to look at.

### 4. Run the linear baseline

One `agent.agents.run_linear` trajectory per task (ReAct loop, `max_calls=30`, temperature 0.6, fixed seed), the
final workspace graded by the hidden grader. Tasks run in parallel threads; each thread is one task.

```bash
python3 scripts/run_linear_baseline.py --workers 8 --max-calls 30 --seed 0
```

- Results: `results/linear_baseline_v2/<task>.json` (trace, full message transcript, final workspace files, hidden
  grade, OpenRouter usage/cost per task) written as each task finishes, and `results/linear_baseline_v2/summary.txt`
  rewritten after every task. `results/` is gitignored.
- Resume after an interruption: run the same command again; tasks that already have a `<task>.json` are skipped
  (`--force` redoes them). A task that crashed leaves `<task>.error.txt` and is retried on the next run.
- Budget: Ripple calls take ~40 s with hidden reasoning, so a 30-call task is up to ~20 min; 50 tasks at 8 workers is
  roughly 2 h. `--deadline-min 110` stops starting new tasks after 110 min (running ones finish).
- Rate limits: the client retries 429/5xx with exponential back-off (2 s -> 60 s, 6 attempts); reduce `--workers` if
  you still see errors.
- Print the summary table (no API calls): `python3 scripts/run_linear_baseline.py --summarize`
  or `cat results/linear_baseline_v2/summary.txt`.

Quick single-task check:

```bash
python3 scripts/run_linear_baseline.py --tasks G_fifo_01 --workers 1
```
