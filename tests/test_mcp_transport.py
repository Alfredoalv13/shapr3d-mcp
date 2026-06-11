"""End-to-end test over the real MCP stdio transport.

Spawns the server as a subprocess and talks JSON-RPC to it with the mcp
client library — the same path Claude Code / Claude Desktop use. Anything
that corrupts stdout (e.g. OCCT chatter) breaks the session itself, so this
also guards transport purity.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

WORKDIR = Path(os.environ["SHAPR3D_MCP_WORKDIR"])

EXPECTED_TOOLS = {
    "create_model", "modify_model", "inspect_model", "convert_model",
    "render_preview", "list_models", "open_in_shapr3d", "shapr3d_status",
    "activate_shapr3d", "screenshot_shapr3d", "build123d_guide",
}


def _payload(result) -> dict:
    if result.structuredContent is not None:
        sc = result.structuredContent
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(result.content[0].text)


def test_tools_over_real_stdio_transport():
    from build123d import Box
    from OCP.IGESControl import IGESControl_Writer

    iges = WORKDIR / "transport_probe.iges"
    writer = IGESControl_Writer()
    writer.AddShape(Box(10, 10, 10).wrapped)
    assert writer.Write(str(iges))

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-c", "from shapr3d_mcp import main; main()"],
            env={**os.environ, "SHAPR3D_MCP_WORKDIR": str(WORKDIR)},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                tools = await session.list_tools()
                assert EXPECTED_TOOLS <= {t.name for t in tools.tools}

                # IGES first, before any export has run in the server
                # process: OCCT reader chatter on stdout would kill the
                # JSON-RPC stream right here if it regressed.
                res = await session.call_tool(
                    "inspect_model", {"path": "transport_probe.iges"}
                )
                assert not res.isError
                assert _payload(res)["bounding_box_mm"]["size"] == [10, 10, 10]

                res = await session.call_tool(
                    "create_model",
                    {"name": "transport_box", "script": "result = Box(10, 10, 10)"},
                )
                assert not res.isError
                payload = _payload(res)
                assert payload["stats"]["volume_mm3"] == 1000
                assert Path(payload["files"][0]).exists()

    asyncio.run(run())
