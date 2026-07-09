"""
Security middleware subpackage.

Layout:
  security/
    __init__.py           (this file — re-exports + submodules)
    cors.py               (CORS configuration, PR-16)
    tls.py                (HTTPS redirect + HSTS + secure-cookie checker, PR-16)

Legacy: src/agent_system/core/security.py holds TrustLevel, InputValidationResult,
AuditLogEntry, InputSanitizer, AuditLogger. We load it via importlib.util to avoid
the file-vs-directory name shadow (the directory wins for the `agent_system.core.security`
namespace, but the file is still importable by its file path).
"""
import importlib.util
from pathlib import Path

_LEGACY_PATH = Path(__file__).resolve().parent.parent / "security.py"

def _load_legacy_module():
    """Load src/agent_system/core/security.py by file path to avoid the
    name clash between the security.py file and this security/ directory.
    """
    spec = importlib.util.spec_from_file_location(
        "_agent_system_core_security_legacy", _LEGACY_PATH,
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load legacy security module from {_LEGACY_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

_legacy = _load_legacy_module()

# Re-export for backward compat
TrustLevel = _legacy.TrustLevel
InputValidationResult = _legacy.InputValidationResult
AuditLogEntry = _legacy.AuditLogEntry
InputSanitizer = _legacy.InputSanitizer
AuditLogger = _legacy.AuditLogger
sanitizer = _legacy.InputSanitizer
audit_logger = _legacy.AuditLogger

# New submodules (PR-16)
from agent_system.core.security.cors import build_cors_config, CORSConfig, is_origin_allowed  # noqa: E402
from agent_system.core.security.tls import (  # noqa: E402
    HTTPSRedirectMiddleware,
    HSTSHeaderMiddleware,
    SecureCookieChecker,
    install_tls_middlewares,
)

__all__ = [
    "TrustLevel", "InputValidationResult", "AuditLogEntry",
    "InputSanitizer", "AuditLogger",
    "sanitizer", "audit_logger",
    "build_cors_config", "CORSConfig", "is_origin_allowed",
    "HTTPSRedirectMiddleware", "HSTSHeaderMiddleware", "SecureCookieChecker",
    "install_tls_middlewares",
]
