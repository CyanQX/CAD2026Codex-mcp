"""A tiny *real* stdio MCP server used to exercise the gateway's session handling.

Run as ``python fake_mcp_server.py``.  It mimics the behaviours of the CAD backends that matter
to the gateway: a per-process call counter (to prove session reuse), slow calls, crashes,
tool errors, JSON-in-text results and image results.
"""

import base64
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, ImageContent, TextContent

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)

mcp = MCPServer("fake-cad-backend")
_lock = threading.Lock()
_calls = 0


@mcp.tool()
def whoami() -> dict[str, Any]:
    """Return this process id and how many calls it has served (proves session reuse)."""
    global _calls
    with _lock:
        _calls += 1
        n = _calls
    return {"ok": True, "pid": os.getpid(), "n": n}


@mcp.tool()
def sleep_for(seconds: float) -> dict[str, Any]:
    time.sleep(seconds)
    return {"ok": True, "slept": seconds}


@mcp.tool()
def boom() -> dict[str, Any]:
    raise ValueError("kaboom")


@mcp.tool()
def die() -> dict[str, Any]:
    os._exit(1)


@mcp.tool()
def die_once(marker: str) -> dict[str, Any]:
    """Crash the first time (creating ``marker``), succeed afterwards."""
    path = Path(marker)
    if not path.exists():
        path.write_text("x")
        os._exit(1)
    return {"ok": True, "pid": os.getpid()}


@mcp.tool()
def json_text() -> str:
    """Like best-cad-mcp: returns a JSON *string*."""
    return json.dumps({"success": True, "handle": "2A3", "layer": "A-WALL"})


@mcp.tool()
def wrapped_dict_fail() -> Any:
    """A dict returned through a loosely typed tool: the SDK wraps it as {"result": {...}}."""
    return {"ok": False, "message": "Runtime preflight failed", "data": {"ready": False}}


@mcp.tool()
def soft_fail() -> dict[str, Any]:
    """Like Slacker: transport ok, payload says ok=false."""
    return {"ok": False, "message": "No running AutoCAD session was found."}


@mcp.tool()
def with_image() -> CallToolResult:
    """Like a screenshot tool: a JSON status block plus a native image block."""
    return CallToolResult(
        content=[
            TextContent(type="text", text=json.dumps({"ok": True, "note": "shot"})),
            ImageContent(type="image", data=base64.b64encode(PNG_1X1).decode(), mime_type="image/png"),
        ]
    )


if __name__ == "__main__":
    mcp.run("stdio")
