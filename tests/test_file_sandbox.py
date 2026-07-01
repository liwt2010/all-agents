"""
Tests: File tools sandbox (BLOCKER 2 fix)
"""

import os
import pytest
from pathlib import Path

from agent_system.tools.file_tools import (
    ReadFileTool, WriteFileTool, ListFilesTool,
    _resolve_and_validate, _get_allowed_roots, DEFAULT_ALLOWED_ROOTS,
)


class TestResolveAndValidate:
    def test_path_inside_cwd_allowed(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()
        allowed = _resolve_and_validate("data/foo.txt", ["data"], allow_write=False)
        assert allowed == (tmp_path / "data" / "foo.txt").resolve()

    def test_path_outside_root_rejected(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(PermissionError, match="outside allowed roots"):
            _resolve_and_validate("/etc/passwd", ["data"], allow_write=False)

    def test_path_traversal_rejected(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()
        with pytest.raises(PermissionError, match="traversal"):
            _resolve_and_validate("../../etc/passwd", ["data"], allow_write=False)

    def test_sensitive_file_writes_rejected(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()
        for name in [".env", "secret.key", "credentials.pem", ".aws/credentials"]:
            with pytest.raises(PermissionError, match="[sS]ensitive"):
                _resolve_and_validate(f"data/{name}", ["data"], allow_write=True)


class TestReadFileTool:
    @pytest.mark.asyncio
    async def test_read_inside_sandbox(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()
        f = tmp_path / "data" / "hello.txt"
        f.write_text("hello world")
        tool = ReadFileTool()
        result = await tool.execute({"path": "data/hello.txt"})
        assert result.success is True
        assert result.output == "hello world"

    @pytest.mark.asyncio
    async def test_read_outside_sandbox_rejected(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        tool = ReadFileTool()
        result = await tool.execute({"path": "/etc/passwd"})
        assert result.success is False
        assert "outside allowed roots" in result.error

    @pytest.mark.asyncio
    async def test_read_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()
        tool = ReadFileTool()
        result = await tool.execute({"path": "data/missing.txt"})
        assert result.success is False

    @pytest.mark.asyncio
    async def test_read_capped_at_1mb(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()
        f = tmp_path / "data" / "big.txt"
        f.write_text("x" * 2_000_000)
        tool = ReadFileTool()
        result = await tool.execute({"path": "data/big.txt"})
        assert result.success is True
        assert "truncated" in result.output


class TestWriteFileTool:
    @pytest.mark.asyncio
    async def test_write_inside_sandbox(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()
        tool = WriteFileTool()
        result = await tool.execute({"path": "data/new.txt", "content": "hello"})
        assert result.success is True
        assert (tmp_path / "data" / "new.txt").read_text() == "hello"

    @pytest.mark.asyncio
    async def test_write_outside_sandbox_rejected(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        tool = WriteFileTool()
        result = await tool.execute({"path": "/etc/passwd", "content": "evil"})
        assert result.success is False

    @pytest.mark.asyncio
    async def test_write_creates_parent_dirs(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()
        tool = WriteFileTool()
        result = await tool.execute({"path": "data/sub/dir/file.txt", "content": "x"})
        assert result.success is True
        assert (tmp_path / "data" / "sub" / "dir" / "file.txt").exists()

    @pytest.mark.asyncio
    async def test_write_rejected_for_env_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()
        tool = WriteFileTool()
        result = await tool.execute({"path": "data/.env", "content": "SECRET=x"})
        assert result.success is False
        assert "sensitive" in result.error.lower()

    @pytest.mark.asyncio
    async def test_write_rejected_for_key_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()
        tool = WriteFileTool()
        result = await tool.execute({"path": "data/server.key", "content": "x"})
        assert result.success is False


class TestListFilesTool:
    @pytest.mark.asyncio
    async def test_list_within_sandbox(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()
        for name in ["a.txt", "b.py", "c.log"]:
            (tmp_path / "data" / name).write_text("x")
        tool = ListFilesTool()
        result = await tool.execute({"path": "data"})
        assert result.success is True
        assert set(result.output["files"]) == {"a.txt", "b.py", "c.log"}

    @pytest.mark.asyncio
    async def test_list_with_pattern(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()
        for name in ["a.txt", "b.py", "c.log"]:
            (tmp_path / "data" / name).write_text("x")
        tool = ListFilesTool()
        result = await tool.execute({"path": "data", "pattern": "*.py"})
        assert result.success is True
        assert result.output["files"] == ["b.py"]

    @pytest.mark.asyncio
    async def test_list_outside_sandbox_rejected(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        tool = ListFilesTool()
        result = await tool.execute({"path": "/etc"})
        assert result.success is False


class TestAllowedRoots:
    def test_default_roots(self, monkeypatch):
        monkeypatch.delenv("ALLOWED_FILE_ROOTS", raising=False)
        assert _get_allowed_roots() == DEFAULT_ALLOWED_ROOTS

    def test_env_roots(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_FILE_ROOTS", "data,scratch,work")
        assert _get_allowed_roots() == ["data", "scratch", "work"]
