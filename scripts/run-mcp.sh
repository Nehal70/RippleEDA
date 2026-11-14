#!/usr/bin/env bash
# Launch one mcp4eda server inside Ubuntu, where the local Docker daemon lives.
export PATH="${HOME}/.local/bin:/usr/bin:/bin:${HOME}/.local/node/bin:${PATH}"
export LD_LIBRARY_PATH="${HOME}/.local/opt/klayout/usr/lib/klayout:${HOME}/.local/opt/klayout-libs/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
export YOSYS_PATH=/usr/bin/yosys
export GTKWAVE_PATH=/usr/bin/gtkwave
export KLAYOUT_PATH="${HOME}/.local/bin/klayout"
export CONTAINER_EXECUTABLE=docker
export OPENLANE_IMAGE=efabless/openlane:latest
export OPENLANE_PDK_VOLUME=ripple-eda-pdks
export PDK_ROOT=/root/.volare
export PDK=sky130A
cd "$1"
shift
exec "$@"
