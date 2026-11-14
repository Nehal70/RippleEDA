# RippleEDA local setup

Follow this document from top to bottom on a Windows machine with Ubuntu 24.04 in WSL2 and Cursor installed on Windows. The result is the same two tracks that exist on the reference machine: native Verilator and Yosys in this repository, and the mcp4eda servers registered in Cursor.

Do not commit `.venv`, `.env`, `mcp4eda/`, `.cursor/mcp.json`, or OpenLane `runs/` directories. Those are local installs and build output.

`https://github.com/mcp4eda/mcp4eda` does not exist. Clone `https://github.com/ssql2014/mcp4eda`. The OpenLane image tag `efabless/openlane:current` does not exist. Use `efabless/openlane:latest`.

## What belongs where

This repository contains the project code and the instructions to reproduce the machine:

- `tools/eda_tools_native.py` calls Verilator and Yosys directly.
- `tools/eda_mcp_server.py` is a small project-owned OpenLane server. The Cursor tools the partner should use are the mcp4eda servers, not this file.
- `core/mcts_prm.py` is the search and scoring code.
- `examples/` holds the counter used to prove both tracks.
- `patches/mcp4eda-local.patch` makes the pinned mcp4eda checkout compile and mount the local sky130 volume.

Installed outside the repository, per user:

- Ubuntu packages, the Ubuntu Docker daemon, and the OpenLane image.
- The sky130 PDK in the Docker volume `ripple-eda-pdks`.
- Linux Node at `~/.local/node`.
- Verible, CBMC, and KLayout under `~/.local`.
- A checkout of mcp4eda next to this file, at `mcp4eda/`.

## 1. Ubuntu packages

Run this inside Ubuntu. It needs the sudo password.

```bash
sudo apt update
sudo apt install -y verilator yosys iverilog gtkwave docker.io python3-pip python3-venv python3.12-venv
sudo usermod -aG docker "$USER"
```

Open a new Ubuntu shell so the docker group applies, then confirm the daemon is the one inside Ubuntu:

```bash
docker info --format '{{.ServerVersion}} {{.OperatingSystem}}'
verilator --version
yosys -V
iverilog -V
gtkwave --version
```

`OperatingSystem` must be Ubuntu. Docker Desktop's Windows engine is not the daemon this setup uses.

## 2. Python environment

From the repository root, inside Ubuntu:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements.txt
cp .env.example .env
```

`.env` holds placeholders for `OPENROUTER_API_KEY`, `RIPPLE_MODEL`, and `PRM_JUDGE_MODEL`. The deterministic scoring in `core/mcts_prm.py` does not call a model. Fill in the key only when the LLM judge is wired up.

The installed `mcp` package is 2.x. `tools/eda_mcp_server.py` imports `MCPServer` from that version. The mcp4eda Python servers (`cbmc-mcp`, `c2rtl-verify-mcp`) still import the 1.x names (`mcp.server.Server`, `mcp.server.stdio`). Those imports succeed on the 2.3 release installed from `requirements.txt`. Do not pin `mcp<2` in this virtualenv, or the project server breaks.

## 3. Linux Node

Cursor on Windows must launch the servers inside Ubuntu, because that is where `docker` talks to the local daemon. Windows Node rewrites `/mnt/c/...` paths and will not see that daemon. Install a Linux Node under the Ubuntu home directory:

```bash
mkdir -p "$HOME/.local"
curl -fsSL -o /tmp/node.tar.xz https://nodejs.org/dist/v22.22.0/node-v22.22.0-linux-x64.tar.xz
tar -xJf /tmp/node.tar.xz -C /tmp
rm -rf "$HOME/.local/node"
mv /tmp/node-v22.22.0-linux-x64 "$HOME/.local/node"
"$HOME/.local/node/bin/node" -v
```

Expected version: `v22.22.0`.

## 4. mcp4eda checkout and patch

From the repository root:

```bash
git clone https://github.com/ssql2014/mcp4eda.git mcp4eda
git -C mcp4eda checkout 178bee2021b269745c212e4db34c8830a113d9ab
git -C mcp4eda apply ../patches/mcp4eda-local.patch
```

The patch does three things:

- `openlane-mcp` reads `CONTAINER_EXECUTABLE`, `OPENLANE_IMAGE`, `OPENLANE_PDK_VOLUME`, `PDK_ROOT`, and `PDK`, and mounts the sky130 volume into every container run.
- `c2rtl-verify-mcp/server.py` changes one illegal `elif` to `if` so the file compiles.
- `rtl-parser-mcp` gains the missing `VeribleASTAnalyzer` module so `tsc` succeeds.

Build every TypeScript server with the Linux Node. `openlane-mcp` is included.

```bash
export PATH="$HOME/.local/node/bin:$PATH"
cd mcp4eda
for dir in yosys-mcp verilator-mcp verible-mcp gtkwave-mcp klayout-mcp anysilicon semiconductor-supply-chain-mcp rtl-parser-mcp openlane-mcp; do
  echo "==== $dir ===="
  (cd "$dir" && npm install && npm run build)
done
```

`anysilicon` and `semiconductor-supply-chain-mcp` download Puppeteer. That step is large and is part of a complete install. `cbmc-mcp` and `c2rtl-verify-mcp` are Python and use the virtualenv from step 2. They have no npm build.

## 5. Verible, CBMC, and KLayout

These three are not installed by the apt line above. Put them in the Ubuntu user's home so sudo is not required.

Verible:

```bash
mkdir -p "$HOME/.local/bin" /tmp/mcp4eda-bins
curl -fL -o /tmp/mcp4eda-bins/verible.tar.gz \
  https://github.com/chipsalliance/verible/releases/download/v0.0-4298-g085eef05/verible-v0.0-4298-g085eef05-linux-static-x86_64.tar.gz
rm -rf /tmp/mcp4eda-bins/verible
mkdir /tmp/mcp4eda-bins/verible
tar -xzf /tmp/mcp4eda-bins/verible.tar.gz -C /tmp/mcp4eda-bins/verible --strip-components=1
cp -a /tmp/mcp4eda-bins/verible/bin/. "$HOME/.local/bin/"
"$HOME/.local/bin/verible-verilog-syntax" --version
```

CBMC 6.11.0 for Ubuntu 24.04:

```bash
curl -fL -o /tmp/mcp4eda-bins/cbmc.deb \
  https://github.com/diffblue/cbmc/releases/download/cbmc-6.11.0/ubuntu-24.04-cbmc-6.11.0-Linux.deb
rm -rf /tmp/mcp4eda-bins/cbmc
mkdir /tmp/mcp4eda-bins/cbmc
dpkg-deb -x /tmp/mcp4eda-bins/cbmc.deb /tmp/mcp4eda-bins/cbmc
cp -a /tmp/mcp4eda-bins/cbmc/usr/bin/. "$HOME/.local/bin/"
"$HOME/.local/bin/cbmc" --version
```

KLayout 0.30.13. The `.deb` needs Qt and Ruby libraries extracted beside it, because they are not installed system-wide:

```bash
curl -fL -o /tmp/mcp4eda-bins/klayout.deb \
  https://www.klayout.org/downloads/Ubuntu-24/klayout_0.30.13-1_amd64.deb
rm -rf "$HOME/.local/opt/klayout"
mkdir -p "$HOME/.local/opt/klayout"
dpkg-deb -x /tmp/mcp4eda-bins/klayout.deb "$HOME/.local/opt/klayout"
ln -sfn "$HOME/.local/opt/klayout/usr/bin/klayout" "$HOME/.local/bin/klayout"

mkdir -p /tmp/mcp4eda-bins/qt "$HOME/.local/opt/klayout-libs"
cd /tmp/mcp4eda-bins/qt
pkgs=(
  libqt5core5t64 libqt5designer5 libqt5gui5t64 libqt5multimedia5 libqt5multimediawidgets5
  libqt5network5t64 libqt5opengl5t64 libqt5printsupport5t64 libqt5sql5t64 libqt5svg5
  libqt5widgets5t64 libqt5xml5t64 libqt5xmlpatterns5 zlib1g libgit2-1.7 libruby3.2
  libqt5dbus5t64 libdouble-conversion3 libicu74 libpcre2-16-0 libmd4c0 libpng16-16t64
  libharfbuzz0b libfreetype6 libhttp-parser2.9 libssh2-1t64
)
for pkg in "${pkgs[@]}"; do apt-get download "$pkg"; done
for deb in *.deb; do dpkg-deb -x "$deb" "$HOME/.local/opt/klayout-libs"; done

export LD_LIBRARY_PATH="$HOME/.local/opt/klayout/usr/lib/klayout:$HOME/.local/opt/klayout-libs/usr/lib/x86_64-linux-gnu"
"$HOME/.local/bin/klayout" -v -b
```

`klayout -v` should print `KLayout 0.30.13`. `scripts/run-mcp.sh` sets `LD_LIBRARY_PATH` the same way, so Cursor does not need this export afterward.

## 6. OpenLane image and sky130

```bash
docker pull efabless/openlane:latest
docker volume create ripple-eda-pdks
docker run --rm \
  -v ripple-eda-pdks:/root/.volare \
  --entrypoint bash efabless/openlane:latest \
  -c 'meta=$(find /nix/store -path "*openlane1-1.1.1/bin/dependencies/tool_metadata.yml" | head -n 1); volare enable -f "$meta"; volare path; ls -d /root/.volare/sky130A'
```

The reference install enabled sky130 commit `bdc9412b3e468c102d01b7cf6337be06ec6e9c9a`. `volare path` prints `/root/.volare`. The volume is about 2.4 GB after a successful enable.

The image's `flow.tcl` is on `PATH` inside the container. A full RTL-to-GDSII run of `examples/counter_ol` completed on the reference machine only after the die was set to `0 0 200 200` with an explicit power-grid pitch. A tiny default die fails in power-grid generation (`PDN-0175`). Leave that config as it is in the example.

## 7. Register the servers in Cursor

Inside Ubuntu, from the repository root:

```bash
bash scripts/write_cursor_mcp.sh
```

That writes `.cursor/mcp.json`. On this layout the command is `wsl.exe`, and every argument after it is a Linux path. The servers are `yosys`, `verilator`, `verible`, `gtkwave`, `klayout`, `openlane`, `rtl-parser`, `anysilicon`, `semiconductor-supply-chain`, `cbmc`, and `c2rtl-verify`.

Reload the Cursor window after the file is written. Cursor does not pick up a new MCP file until then.

If Cursor is running on Linux instead of Windows, do not use `wsl.exe`. Point each server at `bash scripts/run-mcp.sh <server-directory> <node-or-python> <entry>`.

## 8. Checks

Native tools, from the repository root with the virtualenv active:

```bash
python - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, ".")
from tools.eda_tools_native import run_verilator_lint, run_yosys_synth
rtl = "examples/counter.v"
lint = run_verilator_lint(rtl)
synth = run_yosys_synth(rtl)
assert lint["status"] == 0, lint["log"]
assert synth["status"] == 0 and synth["total_cells"] > 0, synth["log"]
print("native_ok", synth["total_cells"], "cells")
PY
```

Each MCP process should stay running until it is killed, because it is waiting on stdio. `timeout` exits 124 in that case:

```bash
bash scripts/run-mcp.sh "$PWD/mcp4eda/yosys-mcp" "$HOME/.local/node/bin/node" dist/index.js
```

Repeat that shape for the other directories. Python servers use `.venv/bin/python` and `server.py`. A nonzero exit other than 124 means the process crashed; read stderr before continuing.

A full OpenLane flow is optional. It takes a few minutes and writes a GDS file under `examples/counter_ol/runs/`. That directory is gitignored.

## Reference pins

| Piece | Pin |
|---|---|
| mcp4eda | `178bee2021b269745c212e4db34c8830a113d9ab` |
| Node | 22.22.0, Linux x64, `~/.local/node` |
| Verible | `v0.0-4298-g085eef05` static Linux tarball |
| CBMC | 6.11.0 Ubuntu 24.04 `.deb` |
| KLayout | 0.30.13 Ubuntu 24 `.deb` plus the libraries in step 5 |
| OpenLane image | `efabless/openlane:latest` (OpenLane 1.1.1) |
| PDK volume | `ripple-eda-pdks`, mount `/root/.volare`, PDK `sky130A` |
