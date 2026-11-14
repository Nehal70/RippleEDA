#!/bin/bash
# Environment for HARNESS runs: uses the second OpenRouter account in .env.harness so the tree search
# and the linear baseline (which reads .env) draw on separate budgets and rate limits.
#   source scripts/harness_env.sh && python3 scripts/run_core_mcts.py ...
_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
_k="$(tr -d '\r' < "$_root/.env.harness" | sed -n 's/^OPENROUTER_API_KEY=//p' | head -1)"
if [ -z "$_k" ]; then echo "[harness] no key in .env.harness" >&2; return 1 2>/dev/null || exit 1; fi
export OPENROUTER_API_KEY="$_k"
unset _k
source "$_root/env_mcts.sh"
echo "[harness] using .env.harness key ($(printf '%s' "$OPENROUTER_API_KEY" | cut -c1-12)...)"
