#!/usr/bin/env bash
# Write .cursor/mcp.json for this machine. Run it inside Ubuntu.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NODE="${HOME}/.local/node/bin/node"
PY="${ROOT}/.venv/bin/python"
LAUNCH="${ROOT}/scripts/run-mcp.sh"
mkdir -p "${ROOT}/.cursor"

if [[ ! -x "$NODE" ]]; then
  echo "Linux Node is missing at $NODE. Install it before writing the Cursor config." >&2
  exit 1
fi

python3 - "$ROOT" "$NODE" "$PY" "$LAUNCH" <<'PY'
import json, sys
root, node, py, launch = sys.argv[1:5]
node_servers = {
    "yosys": "yosys-mcp",
    "verilator": "verilator-mcp",
    "verible": "verible-mcp",
    "gtkwave": "gtkwave-mcp",
    "klayout": "klayout-mcp",
    "openlane": "openlane-mcp",
    "rtl-parser": "rtl-parser-mcp",
    "anysilicon": "anysilicon",
    "semiconductor-supply-chain": "semiconductor-supply-chain-mcp",
}
servers = {}
for name, folder in node_servers.items():
    servers[name] = {
        "command": "wsl.exe",
        "args": ["-d", "Ubuntu", "--", "bash", launch, f"{root}/mcp4eda/{folder}", node, "dist/index.js"],
    }
for name, folder, script in (
    ("cbmc", "cbmc-mcp", "server.py"),
    ("c2rtl-verify", "c2rtl-verify-mcp", "server.py"),
):
    servers[name] = {
        "command": "wsl.exe",
        "args": ["-d", "Ubuntu", "--", "bash", launch, f"{root}/mcp4eda/{folder}", py, script],
    }
out = f"{root}/.cursor/mcp.json"
with open(out, "w", newline="\n") as fh:
    json.dump({"mcpServers": servers}, fh, indent=2)
    fh.write("\n")
print(out)
PY
