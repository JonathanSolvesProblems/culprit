"""Bridge to DataHub's own MCP server.

Culprit does not reimplement catalog access. It launches `mcp-server-datahub`
over stdio and calls the tools DataHub ships, so search, entity fetch, lineage
and query history all go through DataHub's supported surface.

Mutations are enabled here because writing back to the graph is part of the
product, not an afterthought.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

GMS = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")


def _find_server() -> list[str] | None:
    """Build the command that launches DataHub's MCP server.

    Prefers `python -m mcp_server_datahub` over the console-script executable.
    Running the module through the interpreter already in use is more portable
    (no PATH or Scripts-directory assumptions, identical on POSIX and Windows),
    and it avoids Windows Application Control blocking the unsigned shim that
    pip generates, which fails with WinError 4551.

    Falls back to the executable if the package is not importable here.
    """
    try:
        import mcp_server_datahub  # noqa: F401

        return [sys.executable, "-m", "mcp_server_datahub"]
    except ImportError:
        pass

    scripts_dir = Path(sys.executable).parent
    for name in ("mcp-server-datahub.exe", "mcp-server-datahub"):
        candidate = scripts_dir / name
        if candidate.exists():
            return [str(candidate)]
    found = shutil.which("mcp-server-datahub")
    return [found] if found else None


class DataHubMCP:
    """Synchronous wrapper around the DataHub MCP server."""

    def __init__(self, gms_url: str = GMS, enable_mutations: bool = True) -> None:
        self.gms_url = gms_url
        self.enable_mutations = enable_mutations
        self._loop = asyncio.new_event_loop()
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self.tools: list[dict[str, Any]] = []

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> list[dict[str, Any]]:
        self.tools = self._loop.run_until_complete(self._start())
        return self.tools

    async def _start(self) -> list[dict[str, Any]]:
        command = _find_server()
        if command is None:
            raise RuntimeError(
                "mcp-server-datahub not found. Install it into this environment with:\n"
                "    pip install mcp-server-datahub"
            )
        env = dict(os.environ)
        env["DATAHUB_GMS_URL"] = self.gms_url
        if self.enable_mutations:
            env["TOOLS_IS_MUTATION_ENABLED"] = "true"
        env.setdefault("TOOLS_IS_USER_ENABLED", "true")

        self._stack = AsyncExitStack()
        read, write = await self._stack.enter_async_context(
            stdio_client(
                StdioServerParameters(command=command[0], args=command[1:], env=env)
            )
        )
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        listed = await self._session.list_tools()
        return [
            {
                "name": t.name,
                "description": (t.description or "").strip(),
                "input_schema": t.inputSchema,
            }
            for t in listed.tools
        ]

    def close(self) -> None:
        if self._stack is not None:
            try:
                self._loop.run_until_complete(self._stack.aclose())
            except Exception:  # noqa: BLE001 - teardown is best effort
                pass
        self._stack = None
        self._session = None

    # -- calling -----------------------------------------------------------
    def call(self, name: str, arguments: dict[str, Any]) -> str:
        if self._session is None:
            raise RuntimeError("MCP session not started; call start() first")
        return self._loop.run_until_complete(self._call(name, arguments))

    async def _call(self, name: str, arguments: dict[str, Any]) -> str:
        assert self._session is not None
        result = await self._session.call_tool(name, arguments)
        parts: list[str] = []
        for block in result.content:
            text = getattr(block, "text", None)
            parts.append(text if text is not None else str(block))
        body = "\n".join(parts) if parts else "(no content)"

        # MCP reports tool failures by setting isError on an otherwise normal
        # response rather than raising. Without this check a failed write-back
        # is indistinguishable from a successful one, which is a worse failure
        # mode than crashing: the caller reports success and nothing was written.
        if getattr(result, "isError", False):
            raise RuntimeError(f"MCP tool {name!r} failed: {body}")
        return body

    def __enter__(self) -> "DataHubMCP":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
