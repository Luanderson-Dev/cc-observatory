"""Minimal MCP server whose only tool asks the user for input (MCP elicitation).

Used by the ``elicitation-idle`` interactive scenario. It needs the MCP SDK 1.x, which is
not a dev dependency, so Claude Code starts it with ``uv run --with "mcp<2"`` (see the MCP
config in ``tools/capture/README.md``).
"""

# No ``from __future__ import annotations`` here: FastMCP finds the Context parameter
# from the runtime annotation.
from mcp.server.fastmcp import Context, FastMCP
from pydantic import create_model

SERVER_NAME = "spike-elicit"

# A model built with create_model instead of a BaseModel subclass, because pydantic has
# no type information in this project (it is not installed in the dev environment).
Choice = create_model("Choice", color=(str, ...))

server = FastMCP(SERVER_NAME)


async def pick_color(ctx: Context) -> str:
    """Ask the user to pick a color."""
    result = await ctx.elicit(message="Pick a color for the capture spike", schema=Choice)
    return f"action={result.action}"


server.add_tool(pick_color)

if __name__ == "__main__":
    server.run()
