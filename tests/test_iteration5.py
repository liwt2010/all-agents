"""
Tests: Iteration 5 — Event Bus, Context Isolation, Failure UX
"""

import asyncio
import pytest
from datetime import datetime, timezone

from agent_system.core.event_bus import (
    EventBus,
    Event,
    EventCategory,
    EventSeverity,
    make_event,
    subscribe_to_agent_events,
)
from agent_system.core.access_control import (
    AccessControl,
    Resource,
    UserContext,
    SpaceVisibility,
    SpaceContext,
)
from agent_system.core.failure_ux import (
    TaskCheckpoint,
    StepRecord,
    CheckpointStore,
    FriendlyError,
    ErrorCategory,
    TaskType,
    estimate_task_type,
    get_timeout,
    TimeoutMonitor,
    format_stage,
)


class TestEventBus:
    """Test the new event bus"""

    def test_publish_sync_handler(self):
        bus = EventBus()
        received = []

        def handler(event):
            received.append(event)

        bus.subscribe(handler)
        event = make_event(EventCategory.AGENT, "task.started", "test_agent")
        asyncio.run(bus.publish(event))

        assert len(received) == 1
        assert received[0].name == "task.started"
        assert received[0].source == "test_agent"

    def test_wildcard_subscription(self):
        bus = EventBus()
        received = []
        bus.subscribe(lambda e: received.append(e), event_name="agent.task.*")

        asyncio.run(bus.publish(
            make_event(EventCategory.AGENT, "agent.task.started", "a1")
        ))
        asyncio.run(bus.publish(
            make_event(EventCategory.AGENT, "agent.task.completed", "a1")
        ))
        asyncio.run(bus.publish(
            make_event(EventCategory.ESCALATION, "escalation.raised", "a1")
        ))

        assert len(received) == 2  # only the 2 task events match

    def test_category_filter(self):
        bus = EventBus()
        received = []
        bus.subscribe(lambda e: received.append(e), category=EventCategory.ESCALATION)

        asyncio.run(bus.publish(
            make_event(EventCategory.AGENT, "task.started", "a1")
        ))
        asyncio.run(bus.publish(
            make_event(EventCategory.ESCALATION, "escalation.raised", "a2",
                       severity=EventSeverity.WARNING)
        ))

        assert len(received) == 1
        assert received[0].category == EventCategory.ESCALATION

    def test_severity_filter(self):
        bus = EventBus()
        received = []
        bus.subscribe(lambda e: received.append(e), severity_min=EventSeverity.WARNING)

        asyncio.run(bus.publish(make_event(EventCategory.SYSTEM, "info.event", "s1", severity=EventSeverity.INFO)))
        asyncio.run(bus.publish(make_event(EventCategory.SYSTEM, "warn.event", "s1", severity=EventSeverity.WARNING)))
        asyncio.run(bus.publish(make_event(EventCategory.SYSTEM, "err.event", "s1", severity=EventSeverity.ERROR)))

        assert len(received) == 2
        assert received[0].severity == EventSeverity.WARNING

    def test_source_filter(self):
        bus = EventBus()
        received = []
        bus.subscribe(lambda e: received.append(e), source="agent_x")

        asyncio.run(bus.publish(make_event(EventCategory.AGENT, "e1", "agent_x")))
        asyncio.run(bus.publish(make_event(EventCategory.AGENT, "e2", "agent_y")))

        assert len(received) == 1

    def test_unsubscribe(self):
        bus = EventBus()
        received = []
        sid = bus.subscribe(lambda e: received.append(e))
        assert bus.count_subscribers() == 1
        bus.unsubscribe(sid)
        assert bus.count_subscribers() == 0

    def test_event_logging(self, tmp_path):
        bus = EventBus()
        bus.enable_logging(str(tmp_path))

        asyncio.run(bus.publish(
            make_event(EventCategory.AGENT, "task.started", "agent1",
                       data={"task": "test"})
        ))

        log_files = list(tmp_path.glob("*.jsonl"))
        assert len(log_files) >= 1

    def test_convenience_subscribers(self):
        bus = EventBus()
        received = []
        handler = lambda e: received.append(e)

        sid = subscribe_to_agent_events(handler, agent_name="agent_x")
        assert sid.startswith("sub-")


class TestAccessControl:
    """Test the 6-space access control"""

    def test_tenant_isolation(self):
        ac = AccessControl()
        user = UserContext(user_id="u1", tenant_id="tenant_a")
        other_tenant = UserContext(user_id="u2", tenant_id="tenant_b")

        resource = Resource(id="res1", type="task", tenant_id="tenant_a",
                           visibility=SpaceVisibility.TENANT_PUBLIC)
        ac.register_resource(resource)

        assert ac.can_read(user, resource) is True
        assert ac.can_read(other_tenant, resource) is False

    def test_platform_admin_full_access(self):
        ac = AccessControl()
        admin = UserContext(user_id="admin", tenant_id="t1", global_role="platform_admin")
        resource = Resource(id="res1", type="task", tenant_id="t1",
                           owner_id="other_user", visibility=SpaceVisibility.PRIVATE)
        ac.register_resource(resource)
        assert ac.can_read(admin, resource) is True

    def test_owner_full_access(self):
        ac = AccessControl()
        owner = UserContext(user_id="owner", tenant_id="t1")
        resource = Resource(id="res1", type="task", tenant_id="t1",
                           owner_id="owner", visibility=SpaceVisibility.PRIVATE)
        ac.register_resource(resource)
        assert ac.can_read(owner, resource) is True

    def test_private_denied_for_others(self):
        ac = AccessControl()
        owner = UserContext(user_id="owner", tenant_id="t1")
        other = UserContext(user_id="other", tenant_id="t1")

        resource = Resource(id="res1", type="task", tenant_id="t1",
                           owner_id="owner", visibility=SpaceVisibility.PRIVATE)
        ac.register_resource(resource)
        assert ac.can_read(owner, resource) is True
        assert ac.can_read(other, resource) is False

    def test_perm_group_access(self):
        ac = AccessControl()
        member = UserContext(user_id="u1", tenant_id="t1", perm_group_ids=["g1"])
        non_member = UserContext(user_id="u2", tenant_id="t1")

        resource = Resource(id="res1", type="task", tenant_id="t1",
                           visibility=SpaceVisibility.PERM_GROUP,
                           perm_group_ids=["g1"])
        ac.register_resource(resource)
        assert ac.can_read(member, resource) is True
        assert ac.can_read(non_member, resource) is False

    def test_explicit_sharing(self):
        ac = AccessControl()
        user = UserContext(user_id="u1", tenant_id="t1")
        shared = UserContext(user_id="u2", tenant_id="t1")

        resource = Resource(id="res1", type="task", tenant_id="t1",
                           owner_id="u1", visibility=SpaceVisibility.PRIVATE)
        ac.register_resource(resource)
        assert ac.can_read(shared, resource) is False

        ac.share_with_user("res1", "u2")
        assert ac.can_read(shared, resource) is True

        ac.unshare_with_user("res1", "u2")
        assert ac.can_read(shared, resource) is False

    def test_filter_accessible(self):
        ac = AccessControl()
        user = UserContext(user_id="u1", tenant_id="t1")

        r1 = Resource(id="r1", type="task", tenant_id="t1", owner_id="u1",
                      visibility=SpaceVisibility.PRIVATE)
        r2 = Resource(id="r2", type="task", tenant_id="t1", owner_id="other",
                      visibility=SpaceVisibility.TENANT_PUBLIC)
        ac.register_resource(r1)
        ac.register_resource(r2)

        accessible = ac.filter_accessible(user, [r1, r2])
        assert len(accessible) == 2  # owner can read r1, public can read r2

    def test_require_access_raises(self):
        ac = AccessControl()
        user = UserContext(user_id="u1", tenant_id="t1")
        resource = Resource(id="res1", type="task", tenant_id="t1",
                           owner_id="other", visibility=SpaceVisibility.PRIVATE)
        ac.register_resource(resource)
        with pytest.raises(PermissionError):
            ac.require_access(user, resource, "read")

    def test_delete_access(self):
        ac = AccessControl()
        owner = UserContext(user_id="owner", tenant_id="t1")
        other = UserContext(user_id="other", tenant_id="t1")

        resource = Resource(id="res1", type="task", tenant_id="t1",
                           owner_id="owner", visibility=SpaceVisibility.PRIVATE)
        ac.register_resource(resource)

        assert ac.can_delete(owner, resource) is True
        assert ac.can_delete(other, resource) is False

    def test_space_context(self):
        ctx = SpaceContext(
            user=UserContext(user_id="u1", tenant_id="t1"),
            visibility=SpaceVisibility.PRIVATE,
        )
        assert ctx.user.user_id == "u1"
        assert ctx.visibility == SpaceVisibility.PRIVATE

    def test_six_visibilities(self):
        """All 6 space types exist"""
        assert len(SpaceVisibility) == 6
        assert SpaceVisibility.PRIVATE.value == "private"
        assert SpaceVisibility.PERM_GROUP.value == "perm_group"
        assert SpaceVisibility.GROUP.value == "group"
        assert SpaceVisibility.PROJECT.value == "project"
        assert SpaceVisibility.EXTERNAL.value == "external"
        assert SpaceVisibility.TENANT_PUBLIC.value == "tenant_public"


class TestFailureUX:
    """Test failure UX components"""

    def test_error_categorization(self):
        fe = FriendlyError.from_exception(TimeoutError("Connection refused"))
        assert fe.category == ErrorCategory.NETWORK
        assert fe.can_retry is True
        assert len(fe.suggestions) > 0

        fe2 = FriendlyError.from_exception(PermissionError("Access denied"))
        assert fe2.category == ErrorCategory.PERMISSION
        assert fe2.can_retry is False

    def test_friendly_error_structure(self):
        fe = FriendlyError(
            title="Network Error",
            message="Connection to API failed",
            category=ErrorCategory.NETWORK,
            suggestions=["Check network", "Retry later"],
        )
        assert fe.title == "Network Error"
        assert fe.error_code == ""

    def test_friendly_error_code(self):
        fe = FriendlyError.from_exception(RuntimeError("API rate limit exceeded"))
        assert fe.error_code == "ERR-NETWORK"

    def test_checkpoint_lifecycle(self):
        cp = TaskCheckpoint(
            task_id="test-1",
            agent_name="test_agent",
            task_type=TaskType.STANDARD,
            pending_steps=[
                StepRecord(step_id="s1", name="Write PRD"),
                StepRecord(step_id="s2", name="Implement code"),
                StepRecord(step_id="s3", name="Run tests"),
            ],
            timeout_seconds=300,
        )
        assert cp.progress == 0.0
        assert cp.can_resume is True

        cp.complete_step("s1")
        assert cp.progress == pytest.approx(1/3, rel=0.1)
        assert len(cp.completed_steps) == 1
        assert len(cp.pending_steps) == 2

        cp.complete_step("s2")
        assert cp.progress == pytest.approx(2/3, rel=0.1)

        cp.fail_step("s3", "Test failed")
        assert cp.progress == 1.0
        assert len(cp.error_history) >= 1
        assert "Test failed" in cp.error_history[0]["error"]

    def test_checkpoint_serialization(self, tmp_path):
        cp = TaskCheckpoint(
            task_id="serial-test",
            agent_name="test_agent",
            pending_steps=[StepRecord(step_id="s1", name="Step 1")],
        )
        store = CheckpointStore(str(tmp_path))
        assert store.save(cp) is True

        loaded = store.load("serial-test")
        assert loaded is not None
        assert loaded.task_id == "serial-test"
        assert loaded.agent_name == "test_agent"

        store.delete("serial-test")
        assert store.load("serial-test") is None

    def test_timeout_policies(self):
        assert get_timeout(TaskType.QUICK) == 60
        assert get_timeout(TaskType.STANDARD) == 300
        assert get_timeout(TaskType.COMPLEX) == 1800
        assert get_timeout(TaskType.LONG) == 7200
        assert get_timeout(TaskType.BATCH) == 86400

    def test_estimate_task_type(self):
        assert estimate_task_type("batch process all users") == TaskType.BATCH
        assert estimate_task_type("complex end-to-end pipeline") in (TaskType.LONG, TaskType.COMPLEX)
        assert estimate_task_type("quick simple task") == TaskType.QUICK
        assert estimate_task_type("a normal task description with proper length for classification") == TaskType.STANDARD

    def test_checkpoint_expiry(self):
        import time
        cp = TaskCheckpoint(
            task_id="timeout-test",
            agent_name="test",
            timeout_seconds=1,
        )
        cp.start()
        assert cp.is_expired is False
        time.sleep(1.1)
        assert cp.is_expired is True

    def test_checkpoint_resume_context(self):
        cp = TaskCheckpoint(
            task_id="resume-test",
            agent_name="test",
            pending_steps=[StepRecord(step_id="s1", name="S1")],
            intermediate_outputs={"prd": "..."},
            timeout_seconds=300,
        )
        cp.add_error("Temporary failure")
        ctx = cp.to_resume_context()
        assert ctx["task_id"] == "resume-test"
        assert ctx["intermediate_outputs"]["prd"] == "..."
        assert len(ctx["error_history"]) >= 1

    def test_timeout_monitor(self):
        monitor = TimeoutMonitor()
        cp = TaskCheckpoint(
            task_id="expired-task",
            agent_name="test",
            timeout_seconds=0,  # expires immediately
        )
        cp._deadline = 0  # force expired
        monitor.register(cp)

        expired = monitor.check_expired()
        assert "expired-task" in expired

    def test_format_stage(self):
        s = format_stage(1, "Starting execution")
        assert s.stage == 1
        assert s.label == "Executing"
        assert s.progress == 0.0

        s2 = format_stage(3, "Retrying with modifications", progress=0.5)
        assert s2.stage == 3
        assert s2.progress == 0.5

    def test_error_suggestions(self):
        net_err = FriendlyError.from_exception(TimeoutError("timeout"))
        assert len(net_err.suggestions) > 0
        assert "network" in net_err.suggestions[0].lower()

        perm_err = FriendlyError.from_exception(PermissionError("denied"))
        assert len(perm_err.suggestions) > 0

    def test_checkpoint_store_list_active(self, tmp_path):
        store = CheckpointStore(str(tmp_path))
        assert store.list_active() == []

        cp = TaskCheckpoint(task_id="active-1", agent_name="test")
        store.save(cp)
        active = store.list_active()
        assert "active-1" in active
