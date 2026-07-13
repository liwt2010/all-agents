"""
Security — prompt injection protection, input validation, audit logging

ARCHITECTURE.md L2 API Gateway layer:
  - Prompt injection protection
  - Input validation (size/type/sensitive data)
  - Audit logging
  - Trust levels (auto/spot-check/full-review/forbidden)
"""

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class TrustLevel(str, Enum):
    AUTO = "auto"               # Fully trusted, no review
    SPOT_CHECK = "spot_check"   # Random sampling review
    FULL_REVIEW = "full_review" # Human reviews every output
    FORBIDDEN = "forbidden"     # Not allowed


class InputValidationResult(BaseModel):
    """Result of input validation"""
    valid: bool = True
    sanitized: str = ""
    issues: list[str] = Field(default_factory=list)
    risk_level: str = "low"  # low / medium / high / critical


class AuditLogEntry(BaseModel):
    """An audit log entry"""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    user_id: str = ""
    action: str = ""
    resource_id: str = ""
    resource_type: str = ""
    details: dict[str, Any] = Field(default_factory=dict)
    ip_address: str = ""
    user_agent: str = ""
    outcome: str = "success"  # success / failure / denied


# ── Prompt Injection Detection ──

INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|above|prior)",
    r"disregard\s+(all\s+)?(previous|above|prior)",
    r"forget\s+(all\s+)?(previous|above|prior)",
    r"you\s+are\s+(now|not\s+an?\s+ai|free)",
    r"system\s+prompt",
    r"your\s+(instructions|prompt|system\s+message)",
    r"role\s*(:|is)\s*(system|assistant)",
    r"pretend",
    r"act\s+as\s+if",
    r"jailbreak",
    r"you\s+must\s+obey",
    r"new\s+instructions?",
    r"override",
    r"do\s+(not\s+)?(any|what)ever",
    r"hypothetical",
    r"fiction(al)?\s+scenario",
]

SENSITIVE_PATTERNS = [
    r"\bpassword[s]?\b",
    r"\bapi[_-]?key[s]?\b",
    r"\bsecret[s]?\b",
    r"\btoken[s]?\b",
    r"\bcredit\s*card\b",
    r"\bssn\b",
    r"\bsocial\s*security\b",
    r"\bprivate\s*key\b",
    r"\baccess\s*key\b",
    r"\bauth[_-]?token\b",
    r"\bjwt\b",
]

HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
URL_PATTERN = re.compile(r"https?://[^\s]+")


class InputSanitizer:
    """Validates and sanitizes user inputs"""

    def __init__(self, max_input_length: int = 100_000):
        self.max_input_length = max_input_length

    def validate(self, text: str) -> InputValidationResult:
        """Validate and sanitize input text"""
        issues = []

        # 1. Check length
        if len(text) > self.max_input_length:
            return InputValidationResult(
                valid=False,
                sanitized=text[:self.max_input_length],
                issues=[f"Input exceeds max length ({len(text)} > {self.max_input_length})"],
                risk_level="medium",
            )

        sanitized = text

        # 2. Check for prompt injection
        injection_issues = self._detect_injection(text)
        issues.extend(injection_issues)

        # 3. Detect sensitive data
        sensitive_issues = self._detect_sensitive(text)
        issues.extend(sensitive_issues)

        # 4. Assess risk level
        risk_level = self._assess_risk(injection_issues, sensitive_issues)

        # 5. Sanitize sensitive data
        sanitized = self._sanitize_sensitive(sanitized)

        return InputValidationResult(
            valid=risk_level != "critical",
            sanitized=sanitized,
            issues=issues,
            risk_level=risk_level,
        )

    def _detect_injection(self, text: str) -> list[str]:
        """Detect prompt injection attempts"""
        issues = []
        text_lower = text.lower()

        for pattern in INJECTION_PATTERNS:
            if re.search(pattern, text_lower):
                issues.append(f"Possible prompt injection: matched '{pattern}'")

        return issues

    def _detect_sensitive(self, text: str) -> list[str]:
        """Detect sensitive data in input"""
        issues = []
        text_lower = text.lower()

        for pattern in SENSITIVE_PATTERNS:
            if re.search(pattern, text_lower):
                issues.append(f"Possible sensitive data detected: matched '{pattern}'")

        return issues

    def _sanitize_sensitive(self, text: str) -> str:
        """Sanitize sensitive data from text"""
        sanitized = text
        # Mask potential secrets (basic heuristic)
        sanitized = re.sub(r'(api[\s_-]?key\s*[:=]\s*)(\S+)', r'\1[REDACTED]', sanitized, flags=re.IGNORECASE)
        sanitized = re.sub(r'(password\s*[:=]\s*)(\S+)', r'\1[REDACTED]', sanitized, flags=re.IGNORECASE)
        sanitized = re.sub(r'(secret\s*[:=]\s*)(\S+)', r'\1[REDACTED]', sanitized, flags=re.IGNORECASE)
        sanitized = re.sub(r'(token\s*[:=]\s*)(\S+)', r'\1[REDACTED]', sanitized, flags=re.IGNORECASE)
        return sanitized

    def _assess_risk(self, injection_issues: list[str], sensitive_issues: list[str]) -> str:
        """Assess risk level from issues"""
        if len(injection_issues) >= 3:
            return "critical"
        if len(injection_issues) >= 1:
            return "high"
        if len(sensitive_issues) >= 2:
            return "medium"
        if len(sensitive_issues) >= 1:
            return "medium"
        return "low"

    def is_authorized_operation(
        self,
        action: str,
        trust_level: TrustLevel,
    ) -> bool:
        """Check if an operation is authorized at a given trust level"""
        forbidden_actions = [
            "delete_all", "drop_database", "clear_all_data",
            "mass_delete", "bulk_update_all", "override_permissions",
        ]

        if trust_level == TrustLevel.FORBIDDEN:
            return False

        if action in forbidden_actions and trust_level != TrustLevel.AUTO:
            return False

        return True


# ── Audit Logger ──

class AuditLogger:
    """Persistent audit log for all operations"""

    def __init__(self, log_dir: str = "data/audit"):
        self.log_dir = Path(log_dir)

    def log(self, entry: AuditLogEntry) -> bool:
        """Write an audit log entry"""
        self.log_dir.mkdir(parents=True, exist_ok=True)
        date_str = entry.timestamp.strftime("%Y-%m-%d")
        log_file = self.log_dir / f"audit-{date_str}.jsonl"
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(entry.model_dump_json() + "\n")
            return True
        except Exception as e:
            logger.warning(f"Failed to write audit log: {e}")
            return False

    def query(
        self,
        user_id: str | None = None,
        action: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 100,
    ) -> list[AuditLogEntry]:
        """Query audit logs with filters"""
        import json

        results = []
        log_dir = self.log_dir
        if not log_dir.exists():
            return results

        for log_file in sorted(log_dir.glob("audit-*.jsonl"), reverse=True):
            if len(results) >= limit:
                break
            try:
                with open(log_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = AuditLogEntry(**json.loads(line))
                            if user_id and entry.user_id != user_id:
                                continue
                            if action and entry.action != action:
                                continue
                            results.append(entry)
                            if len(results) >= limit:
                                break
                        except Exception:
                            continue
            except Exception:
                continue

        return results


# ── Global instances ──

sanitizer = InputSanitizer()
audit_logger = AuditLogger()
