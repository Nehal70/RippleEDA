# Environment for the MCTS-vs-best-of-3 harness (agent/, eda/, tasks/). `source env_mcts.sh`
_here="$(cd "$(dirname "${BASH_SOURCE[0]:-${(%):-%x}}")" && pwd)"
export RIPPLEEDA_HOME="$_here"
# EDA binaries: OSS CAD Suite (yosys/verilator/iverilog/sby/solvers) + OpenSTA. Override EDA_TOOLS_ROOT on another machine.
export EDA_TOOLS_ROOT="${EDA_TOOLS_ROOT:-$HOME/rippleeda/tools}"
export SKY130_LIB="${SKY130_LIB:-$HOME/rippleeda/pdk/sky130hd/sky130_fd_sc_hd__tt_025C_1v80.lib}"
export NANGATE_LIB="${NANGATE_LIB:-$HOME/rippleeda/pdk/nangate45/nangate45_typ.lib}"
source "$_here/.venv/bin/activate"
export PATH="$_here/.venv/bin:$EDA_TOOLS_ROOT/bin:$EDA_TOOLS_ROOT/oss-cad-suite/bin:$HOME/.local/bin:$PATH"
export PYTHONPATH="$_here:$PYTHONPATH"
# API key: environment first, else the first real (non-placeholder) OPENROUTER_API_KEY in ./.env or ~/rippleeda/.env
if [ -z "$OPENROUTER_API_KEY" ]; then
  for f in "$_here/.env" "$HOME/rippleeda/.env"; do
    k="$(sed -n 's/^OPENROUTER_API_KEY=//p' "$f" 2>/dev/null | head -1)"
    case "$k" in ""|your_*) ;; *) export OPENROUTER_API_KEY="$k"; break;; esac
  done
  unset k
fi
echo "[mcts] harness ready ($( [ -n "$OPENROUTER_API_KEY" ] && echo key set || echo NO KEY ))"
