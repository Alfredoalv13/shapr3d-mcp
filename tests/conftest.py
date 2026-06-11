import os
import tempfile

# WORKDIR is resolved when shapr3d_mcp.server is first imported, so point it
# at a throwaway directory before any test module imports the server.
os.environ["SHAPR3D_MCP_WORKDIR"] = tempfile.mkdtemp(prefix="shapr3d_mcp_tests_")
