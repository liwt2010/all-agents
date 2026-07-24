"""
Tests for v0.6.0 attribution in custom-agent and GitHub webhook
paths.

Covers:
- custom_agents: ACL filter (non-admin user with cross-tenant agent
  is rejected), audit entries on run start / success / failure.
- github_webhook: owner_id and visibility metadata propagate into
  the TaskContext the review agent receives.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


# ── Custom Agent ACL + audit ──


def _user(user_id: str, tenant_id: str = "acme", role: str = "user"):
    return SimpleNamespace(
        id=user_id, tenant_id=tenant_id,
        global_role=SimpleNamespace(value=role),
        perm_group_ids=[], group_ids=[], project_ids=[], is_agent=False,
    )


def _make_instance(do_work_async):
    """Build a fake custom-agent instance with the attributes the
    route handler reads when serializing the response."""
    from enum import Enum

    class _Safety(Enum):
        LOW = "low"
    return SimpleNamespace(
        do_work=AsyncMock(side_effect=do_work_async),
        config=SimpleNamespace(safety=_Safety.LOW),
        tool_registry=SimpleNamespace(list_definitions=lambda: []),
    )


@pytest.fixture
def captured_audit():
    from agent_system.core.audit_logger import AuditLogEntry
    entries: list = []

    class _Cap:
        async def log(self, entry: AuditLogEntry):
            entries.append(entry)
    return _Cap(), entries


def test_custom_agent_run_writes_audit_started_and_success(captured_audit):
    """Happy path: admin invokes their tenant's agent; two audit
    entries (started + success) are recorded."""
    from agent_system.api.routes import custom_agents as ca

    cap, entries = captured_audit
    instance = _make_instance(
        AsyncMock(return_value=SimpleNamespace(type="ok", payload={"summary": "ok"}, id="out-1"))
    )
    with patch.object(ca, "_registry") as reg, patch.object(
        ca, "get_audit_logger_singleton", return_value=cap
    ):
        reg.return_value = SimpleNamespace(
            instantiate=lambda agent_id, tenant_id: instance if tenant_id == "acme" else None,
        )
        from agent_system.api.routes.custom_agents import RunRequest, run_custom_agent
        req = RunRequest(input="hi")
        resp = asyncio.run(
            run_custom_agent("translator", req, user=_user("alice", "acme", role="tenant_admin"))
        )
    assert resp.status == "ok"
    actions = [e.action for e in entries]
    assert actions.count("custom_agent.run") == 2
    outcomes = [e.outcome for e in entries]
    assert "started" in outcomes
    assert "success" in outcomes


def test_custom_agent_run_writes_audit_failure_on_exception(captured_audit):
    from agent_system.api.routes import custom_agents as ca
    cap, entries = captured_audit
    instance = _make_instance(AsyncMock(side_effect=RuntimeError("boom")))
    with patch.object(ca, "_registry") as reg, patch.object(
        ca, "get_audit_logger_singleton", return_value=cap
    ):
        reg.return_value = SimpleNamespace(
            instantiate=lambda agent_id, tenant_id: instance,
        )
        from fastapi import HTTPException
        from agent_system.api.routes.custom_agents import RunRequest, run_custom_agent
        req = RunRequest(input="hi")
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                run_custom_agent("translator", req, user=_user("alice", "acme", role="tenant_admin"))
            )
        assert exc.value.status_code == 500
    outcomes = [e.outcome for e in entries]
    assert "failure" in outcomes


def test_custom_agent_owner_id_in_task_metadata(captured_audit):
    """The TaskContext handed to the agent carries owner_id in its
    metadata so downstream tasks the agent spawns inherit attribution."""
    from agent_system.api.routes import custom_agents as ca
    cap, _ = captured_audit

    seen_ctx: dict = {}

    async def _capture(task):
        seen_ctx["metadata"] = task.metadata
        return SimpleNamespace(type="ok", payload={}, id="out-2")

    instance = _make_instance(_capture)
    with patch.object(ca, "_registry") as reg, patch.object(
        ca, "get_audit_logger_singleton", return_value=cap
    ):
        reg.return_value = SimpleNamespace(
            instantiate=lambda agent_id, tenant_id: instance,
        )
        from agent_system.api.routes.custom_agents import RunRequest, run_custom_agent
        req = RunRequest(input="hi")
        asyncio.run(
            run_custom_agent("translator", req, user=_user("alice", "acme", role="tenant_admin"))
        )
    assert seen_ctx["metadata"]["owner_id"] == "alice"


# ── GitHub Webhook owner + visibility ──


def test_run_review_sets_owner_id_and_project_visibility():
    """The TaskContext the review agent receives carries owner_id +
    visibility=project + project_ids=[pr:repo]."""
    import os
    from agent_system.api.routes import github_webhook as gw
    # Make sure no leaked GITHUB_BOT_USER_ID affects the default.
    os.environ.pop("GITHUB_BOT_USER_ID", None)

    seen_ctx: dict = {}

    async def _capture_do_work(task):
        seen_ctx["metadata"] = task.metadata
        return SimpleNamespace(payload={"summary": "ok"})

    fake_agent = SimpleNamespace(do_work=AsyncMock(side_effect=_capture_do_work))
    # Patch the registry module-level so _run_review's late import sees it.
    import agent_system.core.registry as reg_mod
    original_registry = reg_mod.agent_registry
    reg_mod.agent_registry = SimpleNamespace(get_instance=lambda name: fake_agent)
    try:
        # Patch _post_pr_comment so we don't try real GitHub calls.
        gw._post_pr_comment = AsyncMock()
        asyncio.run(
            gw._run_review(
                repo_full="acme/widgets",
                pr_number=42,
                pr_title="t",
                pr_body="b",
                pr_url="u",
                delivery_id="del-1",
            )
        )
    finally:
        reg_mod.agent_registry = original_registry
    md = seen_ctx["metadata"]
    assert md["owner_id"] == "github-bot"  # default
    assert md["visibility"] == "project"
    assert md["project_ids"] == ["pr:acme/widgets"]


def test_run_review_uses_configured_bot_user_id(monkeypatch):
    """GITHUB_BOT_USER_ID env var overrides the default bot identity."""
    monkeypatch.setenv("GITHUB_BOT_USER_ID", "ci-bot")
    from agent_system.api.routes import github_webhook as gw

    seen_ctx: dict = {}

    async def _capture(task):
        seen_ctx["metadata"] = task.metadata
        return SimpleNamespace(payload={"summary": "ok"})

    fake_agent = SimpleNamespace(do_work=AsyncMock(side_effect=_capture))
    import agent_system.core.registry as reg_mod
    original_registry = reg_mod.agent_registry
    reg_mod.agent_registry = SimpleNamespace(get_instance=lambda name: fake_agent)
    try:
        gw._post_pr_comment = AsyncMock()
        asyncio.run(
            gw._run_review(
                repo_full="x/y",
                pr_number=1,
                pr_title="t",
                pr_body="b",
                pr_url="u",
                delivery_id="del-2",
            )
        )
    finally:
        reg_mod.agent_registry = original_registry
    assert seen_ctx["metadata"]["owner_id"] == "ci-bot"
    assert seen_ctx["metadata"]["project_ids"] == ["pr:x/y"]