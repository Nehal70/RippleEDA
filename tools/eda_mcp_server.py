import os
import subprocess

from mcp.server.mcpserver import MCPServer

# :current was removed from Docker Hub. :latest is the published OpenLane 1 image.
OPENLANE_IMAGE = os.environ.get("OPENLANE_IMAGE", "efabless/openlane:latest")
OPENLANE_PDK_VOLUME = os.environ.get("OPENLANE_PDK_VOLUME", "ripple-eda-pdks")
OPENLANE_PDK_ROOT = os.environ.get("PDK_ROOT", "/root/.volare")

# mcp 2.x renamed FastMCP to MCPServer. The tool decorator and stdio runner match the old API.
mcp = MCPServer("RippleEDA_OpenLane")


@mcp.tool()
def run_openlane_flow(design_dir: str) -> str:
    """
    Executes full RTL-to-GDSII flow via OpenLane Docker container.
    Warning: This can take 5-15 minutes to run.
    """
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{OPENLANE_PDK_VOLUME}:{OPENLANE_PDK_ROOT}",
        "-e", f"PDK_ROOT={OPENLANE_PDK_ROOT}",
        "-e", "PDK=sky130A",
        "-v", f"{design_dir}:/workdir",
        OPENLANE_IMAGE,
        "flow.tcl", "-design", "/workdir",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return f"Flow completed successfully. Log snippet: {result.stdout[-1000:]}"
    except subprocess.CalledProcessError as e:
        log = f"{e.stdout or ''}\n{e.stderr or ''}"
        return f"Flow failed with error: {log[-2000:]}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
