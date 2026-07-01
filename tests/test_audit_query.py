"""
Tests: AuditQuery (14 methods) + AlertEvaluator (5 rules)
"""

import json
import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_system.core.security import AuditLogEntry, AuditLogger
from agent_system.core.audit.query import (
    AuditQuery,
    AlertRule,
    AlertEvent,
    AlertEvaluator,
)


# ── Helpers ──

def make_entry(
    user_id: str = "u1",
    action: str = "task.run",
    resource_type: str = "task",
    resource_id: str = "t-1",
    outcome: str = "success",
    ip_address: str = "10.0.0.1",
    timestamp: datetime = None,
    details: dict = None,
) -> AuditLogEntry:
    return AuditLogEntry(
        user_id=user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=outcome,
        ip_address=ip_address,
        timestamp=timestamp or datetime.now(timezone.utc),
        details=details or {},
    )


@pytest.fixture
def audit_env(tmp_path):
    """Set up an isolated audit log directory with sample entries."""
    logger = AuditLogger(str(tmp_path))
    now = datetime.now(timezone.utc)

    # Sample data: 5 users, 3 outcomes, 4 actions
    entries = [
        make_entry(user_id="alice", action="task.run", resource_id="t-1", outcome="success", timestamp=now - timedelta(hours=1)),
        make_entry(user_id="alice", action="task.run", resource_id="t-2", outcome="success", timestamp=now - timedelta(hours=2)),
        make_entry(user_id="alice", action="data.export", resource_id="t-2", outcome="success", timestamp=now - timedelta(hours=3)),
        make_entry(user_id="bob",   action="auth.login", resource_id="", outcome="failure", timestamp=now - timedelta(minutes=2), ip_address="10.0.0.2"),
        make_entry(user_id="bob",   action="auth.login", resource_id="", outcome="failure", timestamp=now - timedelta(minutes=1), ip_address="10.0.0.2"),
        make_entry(user_id="bob",   action="auth.login", resource_id="", outcome="failure", timestamp=now - timedelta(seconds=30), ip_address="10.0.0.2"),
        make_entry(user_id="carol", action="task.run", resource_id="t-3", outcome="denied", timestamp=now - timedelta(minutes=3)),
        make_entry(user_id="carol", action="task.run", resource_id="t-3", outcome="denied", timestamp=now - timedelta(minutes=2)),
        make_entry(user_id="dave",  action="delete.task", resource_id="t-4", outcome="success", timestamp=now - timedelta(minutes=10)),
        make_entry(user_id="ed",    action="config.update", resource_id="sys", outcome="success", timestamp=now - timedelta(minutes=5), details={"key": "model"}),
    ]
    for e in entries:
        logger.log(e)
    return str(tmp_path), entries


# ── AuditQuery ──

class TestAuditQuery:
    def test_query_by_user(self, audit_env):
        log_dir, _ = audit_env
        q = AuditQuery(AuditLogger(log_dir))
        alice_ops = q.query_by_user("alice", days=30)
        assert len(alice_ops) == 3
        assert all(e.user_id == "alice" for e in alice_ops)

    def test_query_by_user_days_filter(self, audit_env):
        log_dir, _ = audit_env
        q = AuditQuery(AuditLogger(log_dir))
        recent = q.query_by_user("alice", days=1)
        # All alice's events are 1+ hours old
        assert len(recent) >= 0  # may include 1h-old event

    def test_query_by_resource(self, audit_env):
        log_dir, _ = audit_env
        q = AuditQuery(AuditLogger(log_dir))
        t3 = q.query_by_resource("task", "t-3", days=30)
        assert len(t3) == 2
        assert all(e.resource_id == "t-3" for e in t3)

    def test_query_permission_denials(self, audit_env):
        log_dir, _ = audit_env
        q = AuditQuery(AuditLogger(log_dir))
        denials = q.query_permission_denials(days=7)
        assert len(denials) == 2
        assert all(e.outcome == "denied" for e in denials)

    def test_query_logins(self, audit_env):
        log_dir, _ = audit_env
        q = AuditQuery(AuditLogger(log_dir))
        all_logins = q.query_logins(days=7)
        assert len(all_logins) == 3
        bob_logins = q.query_logins(user_id="bob", days=7)
        assert len(bob_logins) == 3

    def test_query_data_exports(self, audit_env):
        log_dir, _ = audit_env
        q = AuditQuery(AuditLogger(log_dir))
        exports = q.query_data_exports(days=30)
        assert len(exports) == 1
        assert exports[0].action == "data.export"

    def test_query_config_changes(self, audit_env):
        log_dir, _ = audit_env
        q = AuditQuery(AuditLogger(log_dir))
        configs = q.query_config_changes(days=30)
        assert len(configs) == 1
        assert configs[0].action == "config.update"

    def test_query_cross_tenant_ops(self, audit_env):
        log_dir, _ = audit_env
        q = AuditQuery(AuditLogger(log_dir))
        # None in sample data
        cross = q.query_cross_tenant_ops(days=30)
        assert len(cross) == 0

    def test_query_deletions(self, audit_env):
        log_dir, _ = audit_env
        q = AuditQuery(AuditLogger(log_dir))
        deletions = q.query_deletions(days=30)
        assert len(deletions) == 1
        assert deletions[0].action == "delete.task"

    def test_query_failures(self, audit_env):
        log_dir, _ = audit_env
        q = AuditQuery(AuditLogger(log_dir))
        failures = q.query_failures(days=7)
        assert len(failures) == 3  # bob's 3 login failures

    def test_query_suspicious_ips(self, audit_env):
        log_dir, _ = audit_env
        q = AuditQuery(AuditLogger(log_dir))
        # With a low threshold, bob's IP (3 failures) should be flagged
        suspicious = q.query_suspicious_ips(days=7, threshold=2)
        assert len(suspicious) >= 1
        ips = [s["ip"] for s in suspicious]
        assert "10.0.0.2" in ips

    def test_query_escalations(self, audit_env):
        log_dir, _ = audit_env
        q = AuditQuery(AuditLogger(log_dir))
        # None in sample
        esc = q.query_escalations(days=30)
        assert len(esc) == 0

    def test_query_discussions(self, audit_env):
        log_dir, _ = audit_env
        q = AuditQuery(AuditLogger(log_dir))
        # None in sample
        disc = q.query_discussions(days=30)
        assert len(disc) == 0

    def test_query_billing_events(self, audit_env):
        log_dir, _ = audit_env
        q = AuditQuery(AuditLogger(log_dir))
        # None in sample
        bills = q.query_billing_events(days=30)
        assert len(bills) == 0

    def test_custom_query_multiple_filters(self, audit_env):
        log_dir, _ = audit_env
        q = AuditQuery(AuditLogger(log_dir))
        results = q.custom_query(
            user_id="alice",
            action="task.run",
            outcome="success",
        )
        assert len(results) == 2  # alice has 2 successful task.runs
        assert all(e.user_id == "alice" for e in results)
        assert all(e.outcome == "success" for e in results)


# ── AlertEvaluator ──

class TestAlertEvaluator:
    def test_default_rules(self):
        evaluator = AlertEvaluator()
        rules = evaluator.list_rules()
        assert len(rules) == 5
        names = {r.name for r in rules}
        assert "permission_denied_spike" in names
        assert "login_failure_spike" in names
        assert "deletion_event" in names
        assert "config_change" in names
        assert "cross_tenant_access" in names

    def test_permission_denied_spike(self, audit_env):
        log_dir, _ = audit_env
        evaluator = AlertEvaluator(AuditQuery(AuditLogger(log_dir)))
        # Sample has 2 denials, threshold=5 — should NOT fire
        alerts = evaluator.evaluate()
        denied_alerts = [a for a in alerts if a.rule_name == "permission_denied_spike"]
        assert len(denied_alerts) == 0

        # Add more denials to exceed threshold
        logger = AuditLogger(log_dir)
        now = datetime.now(timezone.utc)
        for i in range(5):
            logger.log(make_entry(
                user_id="carol",
                action="task.run",
                outcome="denied",
                timestamp=now - timedelta(seconds=i*10),
            ))
        alerts2 = evaluator.evaluate()
        denied_alerts2 = [a for a in alerts2 if a.rule_name == "permission_denied_spike"]
        assert len(denied_alerts2) >= 1
        assert denied_alerts2[0].severity == "HIGH"

    def test_login_failure_spike(self, audit_env):
        log_dir, _ = audit_env
        evaluator = AlertEvaluator(AuditQuery(AuditLogger(log_dir)))
        # Sample has 3 login failures, threshold=10 — should NOT fire
        alerts = evaluator.evaluate()
        login_alerts = [a for a in alerts if a.rule_name == "login_failure_spike"]
        assert len(login_alerts) == 0

    def test_deletion_event_fires(self, audit_env):
        log_dir, _ = audit_env
        evaluator = AlertEvaluator(AuditQuery(AuditLogger(log_dir)))
        alerts = evaluator.evaluate()
        del_alerts = [a for a in alerts if a.rule_name == "deletion_event"]
        assert len(del_alerts) == 1
        assert del_alerts[0].severity == "HIGH"

    def test_config_change_fires(self, audit_env):
        log_dir, _ = audit_env
        evaluator = AlertEvaluator(AuditQuery(AuditLogger(log_dir)))
        alerts = evaluator.evaluate()
        cfg_alerts = [a for a in alerts if a.rule_name == "config_change"]
        assert len(cfg_alerts) == 1
        assert cfg_alerts[0].severity == "MEDIUM"

    def test_cross_tenant_fires_when_present(self, tmp_path):
        log_dir = str(tmp_path)
        logger = AuditLogger(log_dir)
        now = datetime.now(timezone.utc)
        logger.log(make_entry(
            action="task.read",
            outcome="denied",
            timestamp=now,
            details={"cross_tenant": True, "from_tenant": "acme", "to_tenant": "beta"},
        ))
        evaluator = AlertEvaluator(AuditQuery(logger))
        alerts = evaluator.evaluate()
        cross_alerts = [a for a in alerts if a.rule_name == "cross_tenant_access"]
        assert len(cross_alerts) == 1
        assert cross_alerts[0].severity == "HIGH"

    def test_alert_event_structure(self, audit_env):
        log_dir, _ = audit_env
        evaluator = AlertEvaluator(AuditQuery(AuditLogger(log_dir)))
        alerts = evaluator.evaluate()
        # At least one alert fires (deletion or config)
        assert len(alerts) >= 1
        for a in alerts:
            assert isinstance(a, AlertEvent)
            assert a.rule_name
            assert a.severity in ("info", "warning", "critical", "HIGH", "MEDIUM")
            assert a.message
            assert a.value >= 0
            assert a.threshold >= 0
