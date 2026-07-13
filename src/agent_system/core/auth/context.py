"""
Tenant context — PLATFORM §7.7, §28, §29

Provides:
  - TenantContext (Pydantic): current user + their tenant/group
  - ContextVar-based get/set for async code
  - CrossTenantAccessError

This is the foundation that GroupIsolationMixin builds on. The full user
model (User/Tenant/Group/Permission with full RBAC) is built in the
next iteration (task #82); for now we have the minimal shape so the
mixin can compile and tests can run.
"""

from contextvars import ContextVar
from typing import List, Optional

from pydantic import BaseModel, Field


class CrossTenantAccessError(PermissionError):
    """Raised when an agent tries to access a resource in a different tenant."""


class _UserStub(BaseModel):
    """Minimal user shape for context. Full User model is in task #82."""
    user_id: str
    tenant_id: str = "default"
    group_ids: list[str] = Field(default_factory=list)
    perm_group_ids: list[str] = Field(default_factory=list)
    project_ids: list[str] = Field(default_factory=list)
    global_role: str = "user"
    is_agent: bool = False


class TenantContext(BaseModel):
    """The active tenant context for the current async task."""
    user: _UserStub | None = None
    tenant_id: str = "default"
    group_ids: list[str] = Field(default_factory=list)
    request_id: str = ""


# ContextVar for async-safe access
_tenant_ctx: ContextVar[TenantContext | None] = ContextVar("tenant_ctx", default=None)


def get_current_tenant() -> TenantContext | None:
    """Return the current TenantContext (or None if not set)."""
    return _tenant_ctx.get()


def set_tenant_context(ctx: TenantContext | None) -> object:
    """
    Set the current tenant context. Returns a token to reset it.

    Usage:
        token = set_tenant_context(ctx)
        try:
            ...
        finally:
            reset_tenant_context(token)
    """
    return _tenant_ctx.set(ctx)


def reset_tenant_context(token: object) -> None:
    """Reset tenant context to the previous value."""
    _tenant_ctx.reset(token)


class tenant_scope:
    """Context manager that sets/restores tenant context."""

    def __init__(self, ctx: TenantContext):
        self.ctx = ctx
        self.token = None

    def __enter__(self):
        self.token = set_tenant_context(self.ctx)
        return self.ctx

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.token is not None:
            reset_tenant_context(self.token)
        return False
