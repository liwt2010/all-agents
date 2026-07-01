"""
AuditQuery — 14 common queries from PLATFORM §13.3.

Each query is a class method that takes filters and returns a list of
AuditLogEntry. Backed by the on-disk JSONL files written by AuditLogger.
"""

import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from agent_system.core.security import AuditLogEntry, AuditLogger

logger = logging.getLogger(__name__)


class AuditQuery:
    """
    14 standard audit-log queries.

    The full list is in PLATFORM.md §13.3. Each method returns a list of
    matching AuditLogEntry sorted by timestamp (newest first by default).
    """

    def __init__(self, audit_logger: Optional[AuditLogger] = None):
        self.logger = audit_logger or AuditLogger()
        self.log_dir = self.logger.log_dir

    def _all_entries(self, since: Optional[datetime] = None, limit: int = 10_000) -> List[AuditLogEntry]:
        """Load all entries (optionally filtered by date)."""
        results = []
        if not self.log_dir.exists():
            return results
        for log_file in sorted(self.log_dir.glob("audit-*.jsonl"), reverse=True):
            try:
                with open(log_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = AuditLogEntry(**json.loads(line))
                            if since and entry.timestamp < since:
                                continue
                            results.append(entry)
                            if len(results) >= limit:
                                return results
                        except Exception:
                            continue
            except Exception:
                continue
        return results

    def _filter(
        self,
        entries: List[AuditLogEntry],
        user_id: Optional[str] = None,
        action: Optional[str] = None,
        outcome: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> List[AuditLogEntry]:
        out = entries
        if user_id:
            out = [e for e in out if e.user_id == user_id]
        if action:
            out = [e for e in out if e.action == action]
        if outcome:
            out = [e for e in out if e.outcome == outcome]
        if since:
            out = [e for e in out if e.timestamp >= since]
        if until:
            out = [e for e in out if e.timestamp <= until]
        return out

    # ── 1. user operation history ──

    def query_by_user(self, user_id: str, days: int = 30, limit: int = 200) -> List[AuditLogEntry]:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        entries = self._all_entries(since=since, limit=limit)
        return self._filter(entries, user_id=user_id)

    # ── 2. resource access records ──

    def query_by_resource(self, resource_type: str, resource_id: str,
                          days: int = 30) -> List[AuditLogEntry]:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        entries = self._all_entries(since=since)
        out = []
        for e in entries:
            if e.resource_type == resource_type and e.resource_id == resource_id:
                out.append(e)
        return out

    # ── 3. permission denials ──

    def query_permission_denials(self, days: int = 7) -> List[AuditLogEntry]:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        entries = self._all_entries(since=since)
        return [e for e in entries if e.outcome == "denied"]

    # ── 4. login history ──

    def query_logins(self, user_id: Optional[str] = None, days: int = 30) -> List[AuditLogEntry]:
        return self._filter(
            self._all_entries(since=datetime.now(timezone.utc) - timedelta(days=days)),
            user_id=user_id, action="auth.login",
        )

    # ── 5. data exports ──

    def query_data_exports(self, days: int = 30) -> List[AuditLogEntry]:
        return self._filter(
            self._all_entries(since=datetime.now(timezone.utc) - timedelta(days=days)),
            action="data.export",
        )

    # ── 6. config changes ──

    def query_config_changes(self, days: int = 30) -> List[AuditLogEntry]:
        return [
            e for e in self._all_entries(since=datetime.now(timezone.utc) - timedelta(days=days))
            if e.action.startswith("config.")
        ]

    # ── 7. cross-tenant ops ──

    def query_cross_tenant_ops(self, days: int = 30) -> List[AuditLogEntry]:
        return [
            e for e in self._all_entries(since=datetime.now(timezone.utc) - timedelta(days=days))
            if "cross_tenant" in (e.details or {})
        ]

    # ── 8. deletions ──

    def query_deletions(self, days: int = 30) -> List[AuditLogEntry]:
        return [
            e for e in self._all_entries(since=datetime.now(timezone.utc) - timedelta(days=days))
            if e.action.startswith("delete") or e.outcome == "deletion"
        ]

    # ── 9. failed operations ──

    def query_failures(self, days: int = 1) -> List[AuditLogEntry]:
        return self._filter(
            self._all_entries(since=datetime.now(timezone.utc) - timedelta(days=days)),
            outcome="failure",
        )

    # ── 10. suspicious IPs ──

    def query_suspicious_ips(self, days: int = 7, threshold: int = 50) -> List[Dict[str, Any]]:
        """Return IPs that exceed the request threshold (denied + failure events)."""
        since = datetime.now(timezone.utc) - timedelta(days=days)
        entries = self._all_entries(since=since)
        ip_counts: Counter = Counter()
        for e in entries:
            if e.outcome in ("denied", "failure") and e.ip_address:
                ip_counts[e.ip_address] += 1
        return [
            {"ip": ip, "count": count, "threshold": threshold}
            for ip, count in ip_counts.most_common()
            if count >= threshold
        ]

    # ── 11. escalations ──

    def query_escalations(self, days: int = 30) -> List[AuditLogEntry]:
        return self._filter(
            self._all_entries(since=datetime.now(timezone.utc) - timedelta(days=days)),
            action="agent.escalated",
        )

    # ── 12. peer discussions ──

    def query_discussions(self, days: int = 30) -> List[AuditLogEntry]:
        return self._filter(
            self._all_entries(since=datetime.now(timezone.utc) - timedelta(days=days)),
            action="agent.peer.discussion",
        )

    # ── 13. billing events ──

    def query_billing_events(self, days: int = 30) -> List[AuditLogEntry]:
        return [
            e for e in self._all_entries(since=datetime.now(timezone.utc) - timedelta(days=days))
            if e.action.startswith("billing.")
        ]

    # ── 14. custom query ──

    def custom_query(
        self,
        user_id: Optional[str] = None,
        action: Optional[str] = None,
        action_prefix: Optional[str] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        outcome: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        ip_address: Optional[str] = None,
        limit: int = 200,
    ) -> List[AuditLogEntry]:
        """Ad-hoc query with arbitrary filters."""
        since = since or (datetime.now(timezone.utc) - timedelta(days=30))
        entries = self._all_entries(since=since, limit=limit)
        out = []
        for e in entries:
            if user_id and e.user_id != user_id:
                continue
            if action and e.action != action:
                continue
            if action_prefix and not e.action.startswith(action_prefix):
                continue
            if resource_type and e.resource_type != resource_type:
                continue
            if resource_id and e.resource_id != resource_id:
                continue
            if outcome and e.outcome != outcome:
                continue
            if until and e.timestamp > until:
                continue
            if ip_address and e.ip_address != ip_address:
                continue
            out.append(e)
            if len(out) >= limit:
                break
        return out


# ── Audit Alerts (PLATFORM §13.4) ──

class AlertRule(BaseModel):
    """A single audit alert rule."""
    name: str
    severity: str  # info / warning / critical
    window_minutes: int = 60
    threshold: int = 5
    description: str = ""


class AlertEvent(BaseModel):
    """An alert that fired."""
    rule_name: str
    severity: str
    message: str
    value: float
    threshold: float
    window_minutes: int
    triggered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    related_entries: List[str] = Field(default_factory=list)  # entry ids


class AlertEvaluator:
    """
    Audit Alert Evaluator — PLATFORM §13.4.

    5 default rules:
      1. permission_denied_spike  (>=5 denials in 5 min, HIGH)
      2. login_failure_spike      (>=10 failures in 10 min, MEDIUM)
      3. deletion_event           (any delete, HIGH)
      4. config_change            (any config change, MEDIUM)
      5. cross_tenant_access      (any cross-tenant, HIGH)
    """

    DEFAULT_RULES = [
        AlertRule(
            name="permission_denied_spike",
            severity="HIGH",
            window_minutes=5,
            threshold=5,
            description="5+ permission denials in 5 minutes",
        ),
        AlertRule(
            name="login_failure_spike",
            severity="MEDIUM",
            window_minutes=10,
            threshold=10,
            description="10+ login failures in 10 minutes",
        ),
        AlertRule(
            name="deletion_event",
            severity="HIGH",
            window_minutes=0,  # any delete
            threshold=1,
            description="A deletion event occurred",
        ),
        AlertRule(
            name="config_change",
            severity="MEDIUM",
            window_minutes=0,
            threshold=1,
            description="A config change occurred",
        ),
        AlertRule(
            name="cross_tenant_access",
            severity="HIGH",
            window_minutes=0,
            threshold=1,
            description="A cross-tenant access was attempted",
        ),
    ]

    def __init__(self, query: Optional[AuditQuery] = None):
        self.query = query or AuditQuery()
        self.rules: List[AlertRule] = list(self.DEFAULT_RULES)

    def evaluate(self, window_minutes: Optional[int] = None) -> List[AlertEvent]:
        """Run all rules and return fired alerts."""
        alerts: List[AlertEvent] = []
        for rule in self.rules:
            fired = self._evaluate_rule(rule, window_minutes)
            if fired:
                alerts.extend(fired)
        return alerts

    def _evaluate_rule(
        self,
        rule: AlertRule,
        window_minutes: Optional[int],
    ) -> List[AlertEvent]:
        now = datetime.now(timezone.utc)
        if rule.window_minutes > 0:
            wm = window_minutes or rule.window_minutes
            since = now - timedelta(minutes=wm)
        else:
            since = datetime(2000, 1, 1, tzinfo=timezone.utc)  # all time

        entries = self.query._all_entries(since=since)

        if rule.name == "permission_denied_spike":
            return self._check_count_rule(rule, entries, "denied", rule.threshold)
        elif rule.name == "login_failure_spike":
            return self._check_count_rule(rule, entries, "failure", rule.threshold,
                                         action_filter="auth.login")
        elif rule.name == "deletion_event":
            deletions = [e for e in entries
                         if e.action.startswith("delete") or e.outcome == "deletion"]
            if len(deletions) >= rule.threshold:
                return [self._make_alert(rule, len(deletions),
                                          f"{len(deletions)} deletion(s) detected",
                                          entry_ids=[e.timestamp.isoformat() for e in deletions[:5]])]
            return []
        elif rule.name == "config_change":
            configs = [e for e in entries if e.action.startswith("config.")]
            if len(configs) >= rule.threshold:
                return [self._make_alert(rule, len(configs),
                                          f"{len(configs)} config change(s) detected",
                                          entry_ids=[e.timestamp.isoformat() for e in configs[:5]])]
            return []
        elif rule.name == "cross_tenant_access":
            cross = [e for e in entries if "cross_tenant" in (e.details or {})]
            if len(cross) >= rule.threshold:
                return [self._make_alert(rule, len(cross),
                                          f"{len(cross)} cross-tenant attempt(s) detected",
                                          entry_ids=[e.timestamp.isoformat() for e in cross[:5]])]
            return []
        return []

    def _check_count_rule(
        self,
        rule: AlertRule,
        entries: List[AuditLogEntry],
        outcome: str,
        threshold: int,
        action_filter: Optional[str] = None,
    ) -> List[AlertEvent]:
        matched = [e for e in entries if e.outcome == outcome]
        if action_filter:
            matched = [e for e in matched if e.action == action_filter]
        if len(matched) >= threshold:
            return [self._make_alert(
                rule, len(matched),
                f"{len(matched)} {outcome} events (threshold {threshold})",
                entry_ids=[e.timestamp.isoformat() for e in matched[:5]],
            )]
        return []

    def _make_alert(
        self,
        rule: AlertRule,
        value: float,
        message: str,
        entry_ids: Optional[List[str]] = None,
    ) -> AlertEvent:
        return AlertEvent(
            rule_name=rule.name,
            severity=rule.severity,
            message=message,
            value=value,
            threshold=float(rule.threshold),
            window_minutes=rule.window_minutes,
            related_entries=entry_ids or [],
        )

    def list_rules(self) -> List[AlertRule]:
        return list(self.rules)
