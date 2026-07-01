"""
Tests: MCP client + manager (with mocked server, no real subprocess)
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agent_system.tools.mcp_client import (
    MCPClient,
    MCPServerSpec,
    MCPServerManager,
    MCPToolWrapper,
    get_mcp_manager,
)
from agent_system.tools.base import ToolResult


def _mock_session_with_tools(*tool_names: str):
    """Build a fake ClientSession that returns given tool names."""
    session = MagicMock()
    tool_objs = []
    for name in tool_names:
        t = MagicMock()
        t.name = name
        t.description = f"Test tool {name}"
        t.inputSchema = {"type": "object", "properties": {}}
        tool_objs.append(t)
    result = MagicMock()
    result.tools = tool_objs
    session.list_tools = AsyncMock(return_value=result)
    content_item = MagicMock()
    content_item.model_dump = lambda: {"type": "text", "text": "result from server"}
    call_result = MagicMock()
    call_result.content = [content_item]
    session.call_tool = AsyncMock(return_value=call_result)
    return session


# ── MCPClient ──

class TestMCPClient:
    @pytest.mark.asyncio
    async def test_init_does_not_connect(self):
        client = MCPClient("test", "echo", ["hello"])
        assert client._connected is False
        assert client._session is None

    @pytest.mark.asyncio
    async def test_list_tools_not_connected_returns_empty(self):
        client = MCPClient("test", "echo", ["hello"])
        result = await client.list_tools()
        assert result == []

    @pytest.mark.asyncio
    async def test_call_tool_not_connected_raises(self):
        client = MCPClient("test", "echo", ["hello"])
        with pytest.raises(RuntimeError):
            await client.call_tool("x", {})

    @pytest.mark.asyncio
    async def test_list_and_call_with_injected_session(self):
        """Set up the client with a fake session directly (no real subprocess)."""
        client = MCPClient("test", "fake")
        session = _mock_session_with_tools("read_file")
        client._session = session
        client._connected = True
        client._stack = AsyncMock()

        try:
            tools = await client.list_tools()
            assert len(tools) == 1
            assert tools[0]["name"] == "read_file"

            result = await client.call_tool("read_file", {"path": "/x"})
            assert len(result) == 1
            assert result[0]["text"] == "result from server"
        finally:
            await client.disconnect()
        assert client._connected is False


# ── MCPServerManager ──

class TestMCPServerManager:
    def test_add_server(self):
        mgr = MCPServerManager()
        mgr.add_server(MCPServerSpec(
            name="fs", command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", "./data"],
        ))
        assert "fs" in mgr.list_servers()

    @pytest.mark.asyncio
    async def test_connect_unknown_raises(self):
        mgr = MCPServerManager()
        with pytest.raises(ValueError, match="Unknown MCP server"):
            await mgr.connect("nonexistent")

    @pytest.mark.asyncio
    async def test_get_tool_lazy(self):
        """No server added — get_tool should return None."""
        mgr = MCPServerManager()
        assert await mgr.get_tool("nonexistent") is None

    @pytest.mark.asyncio
    async def test_register_then_get_tool(self):
        """Pre-populate a client with a session, then register tools from it."""
        mgr = MCPServerManager()
        client = MCPClient("test", "fake")
        client._session = _mock_session_with_tools("alpha", "beta")
        client._connected = True
        mgr._clients["test"] = client

        await mgr._register_tools("test")
        assert "alpha" in mgr.get_registered_tool_names()
        assert "beta" in mgr.get_registered_tool_names()

        tool = await mgr.get_tool("alpha")
        assert tool is not None
        assert tool.name == "alpha"

    def test_get_registered_tool_names(self):
        mgr = MCPServerManager()
        assert mgr.get_registered_tool_names() == []


# ── MCPToolWrapper ──

class TestMCPToolWrapper:
    @pytest.mark.asyncio
    async def test_execute_success(self):
        client = MCPClient("test", "fake")
        client._connected = True
        client._session = _mock_session_with_tools("dummy")
        wrapper = MCPToolWrapper(client, {
            "name": "dummy",
            "description": "Test",
            "input_schema": {"type": "object", "properties": {"x": {"type": "string"}}},
        })
        result = await wrapper.execute({"x": "hello"})
        assert result.success is True

    @pytest.mark.asyncio
    async def test_execute_error(self):
        client = MCPClient("test", "fake")
        client._connected = True
        client._session = MagicMock()
        client._session.call_tool = AsyncMock(side_effect=RuntimeError("boom"))
        wrapper = MCPToolWrapper(client, {
            "name": "broken", "description": "x", "input_schema": {}
        })
        result = await wrapper.execute({})
        assert result.success is False
        assert "boom" in result.error

    def test_schema_exposed_as_fields(self):
        client = MCPClient("test", "fake")
        client._connected = True
        wrapper = MCPToolWrapper(client, {
            "name": "x", "description": "y", "input_schema": {"type": "object"}
        })
        assert wrapper.name == "x"
        assert wrapper.description == "y"
        assert wrapper.input_schema == {"type": "object"}


# ── Global ──

class TestGlobalMCP:
    def test_singleton(self):
        m1 = get_mcp_manager()
        m2 = get_mcp_manager()
        assert m1 is m2
