"""
MCP server integration (PLATFORM §6, §5.3).

Connects to one or more MCP servers (stdio transport), discovers their
tools/resources/prompts, and registers them as Tool objects in our
existing tool registry.

Two registration modes:
  1. Eager (default): at startup, list tools and wrap each
  2. Lazy: on first call, list tools and wrap them

The wrapping is automatic — each MCP tool becomes a BaseTool whose
execute() forwards to the MCP server call.
"""

import asyncio
import logging
import os
from contextlib import AsyncExitStack
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from agent_system.tools.base import Tool, ToolResult

# Module-level imports for testability (patchable in tests)
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger(__name__)


# ── MCP Client ──

class MCPClient:
    """
    Async client to one MCP server over stdio transport.

    Lifecycle:
        client = MCPClient("npx", ["-y", "@modelcontextprotocol/server-filesystem", "./data"])
        await client.connect()
        try:
            tools = await client.list_tools()
            result = await client.call_tool("read_file", {"path": "/x"})
        finally:
            await client.disconnect()
    """

    def __init__(self, server_name: str, command: str, args: Optional[List[str]] = None,
                 env: Optional[Dict[str, str]] = None):
        self.server_name = server_name
        self.command = command
        self.args = args or []
        self.env = env or {}
        self._stack: Optional[AsyncExitStack] = None
        self._session = None
        self._stdio_ctx = None
        self._connected = False

    async def connect(self):
        """Spawn the MCP server subprocess and initialize the session."""
        if self._connected:
            return

        params = StdioServerParameters(
            command=self.command,
            args=self.args,
            env={**os.environ, **self.env} if self.env else None,
        )

        self._stack = AsyncExitStack()
        try:
            self._stdio_ctx = stdio_client(params)
            read, write = await self._stack.enter_async_context(self._stdio_ctx)
            self._session = ClientSession(read, write)
            await self._stack.enter_async_context(self._session)
            await self._session.initialize()
            self._connected = True
            logger.info(f"MCP client connected: {self.server_name} ({self.command})")
        except Exception as e:
            logger.warning(f"MCP connect failed for {self.server_name}: {e}")
            if self._stack:
                await self._stack.aclose()
                self._stack = None
            self._connected = False
            raise

    async def disconnect(self):
        if self._stack:
            await self._stack.aclose()
            self._stack = None
        self._session = None
        self._connected = False

    async def list_tools(self) -> List[Dict[str, Any]]:
        """Return list of available tools: [{name, description, inputSchema}]"""
        if not self._connected or not self._session:
            return []
        result = await self._session.list_tools()
        return [
            {
                "name": t.name,
                "description": t.description or "",
                "input_schema": t.inputSchema or {},
            }
            for t in result.tools
        ]

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        """Invoke a tool by name and return its content."""
        if not self._connected or not self._session:
            raise RuntimeError(f"MCP client {self.server_name} not connected")
        result = await self._session.call_tool(name, arguments)
        # result.content is a list of TextContent / ImageContent / etc.
        return [c.model_dump() if hasattr(c, "model_dump") else c for c in result.content]

    async def list_resources(self) -> List[Dict[str, Any]]:
        if not self._connected or not self._session:
            return []
        try:
            result = await self._session.list_resources()
            return [
                {"uri": r.uri, "name": r.name, "description": r.description or ""}
                for r in result.resources
            ]
        except Exception as e:
            logger.debug(f"list_resources failed: {e}")
            return []


# ── Tool wrapper: bridges MCP tools to our Tool interface ──

class MCPToolWrapper(Tool):
    """Wraps a remote MCP tool as a local Tool."""

    def __init__(self, client: MCPClient, tool_def: Dict[str, Any]):
        self._client = client
        self._tool_def = tool_def
        self.name = tool_def["name"]
        self.description = tool_def.get("description", "")
        self.input_schema = tool_def.get("input_schema", {})

    async def execute(self, inputs: Dict[str, Any]) -> ToolResult:
        try:
            content = await self._client.call_tool(self.name, inputs)
            return ToolResult(success=True, output=content)
        except Exception as e:
            logger.warning(f"MCP tool {self.name} failed: {e}")
            return ToolResult(success=False, error=str(e)[:500])


# ── Manager ──

class MCPServerSpec(BaseModel):
    """Configuration for one MCP server."""
    name: str
    command: str
    args: List[str] = Field(default_factory=list)
    env: Dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    eager: bool = True  # discover tools at startup vs lazy on first use


class MCPServerManager:
    """
    Manages multiple MCP server connections and registers their tools.
    """

    def __init__(self):
        self._clients: Dict[str, MCPClient] = {}
        self._tool_to_client: Dict[str, str] = {}
        self._registered_tools: Dict[str, MCPToolWrapper] = {}
        self._eager_done: set = set()

    def add_server(self, spec: MCPServerSpec) -> MCPClient:
        """Add a server spec (does not connect yet)."""
        client = MCPClient(
            server_name=spec.name,
            command=spec.command,
            args=list(spec.args),
            env=dict(spec.env),
        )
        self._clients[spec.name] = client
        return client

    async def connect(self, name: str) -> MCPClient:
        """Connect a single server. If eager, registers its tools immediately."""
        client = self._clients.get(name)
        if client is None:
            raise ValueError(f"Unknown MCP server: {name}")
        if not client._connected:
            await client.connect()
        return client

    async def connect_all(self, specs: List[MCPServerSpec]) -> List[Dict[str, Any]]:
        """Connect all enabled servers. Returns discovery results."""
        results = []
        for spec in specs:
            if not spec.enabled:
                continue
            if spec.name not in self._clients:
                self.add_server(spec)
            try:
                await self.connect(spec.name)
                if spec.eager:
                    await self._register_tools(spec.name)
                    self._eager_done.add(spec.name)
                results.append({"server": spec.name, "status": "connected", "eager": spec.eager})
            except Exception as e:
                results.append({"server": spec.name, "status": "failed", "error": str(e)})
        return results

    async def disconnect_all(self):
        for client in self._clients.values():
            try:
                await client.disconnect()
            except Exception as e:
                logger.debug(f"disconnect error: {e}")
        self._clients.clear()
        self._tool_to_client.clear()
        self._registered_tools.clear()
        self._eager_done.clear()

    async def _register_tools(self, server_name: str):
        """Discover tools from a server and register them."""
        client = self._clients[server_name]
        tools = await client.list_tools()
        for t in tools:
            if t["name"] in self._registered_tools:
                continue
            wrapper = MCPToolWrapper(client, t)
            self._registered_tools[t["name"]] = wrapper
            self._tool_to_client[t["name"]] = server_name
            logger.info(f"Registered MCP tool: {server_name}/{t['name']}")

    async def get_tool(self, name: str) -> Optional[MCPToolWrapper]:
        """Get a tool, lazily loading if needed."""
        if name in self._registered_tools:
            return self._registered_tools[name]
        # Try lazy discovery across all connected clients
        for server_name, client in self._clients.items():
            if server_name in self._eager_done:
                continue
            if not client._connected:
                continue
            try:
                await self._register_tools(server_name)
                self._eager_done.add(server_name)
            except Exception:
                continue
        return self._registered_tools.get(name)

    def get_registered_tool_names(self) -> List[str]:
        return list(self._registered_tools.keys())

    def list_servers(self) -> List[str]:
        return list(self._clients.keys())


# ── Singleton ──

_default_manager: Optional[MCPServerManager] = None


def get_mcp_manager() -> MCPServerManager:
    global _default_manager
    if _default_manager is None:
        _default_manager = MCPServerManager()
    return _default_manager


def reset_mcp_manager():
    global _default_manager
    if _default_manager is not None:
        try:
            asyncio.run(_default_manager.disconnect_all())
        except Exception:
            pass
    _default_manager = None
