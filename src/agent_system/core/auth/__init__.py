"""Auth subpackage — TenantContext, User/Tenant/Group models, RBAC, JWT."""
from agent_system.core.auth.context import (
    TenantContext, _UserStub, CrossTenantAccessError,
    set_tenant_context, reset_tenant_context, get_current_tenant, tenant_scope,
)
from agent_system.core.auth.models import (
    GlobalRole, Permission, Tenant, Group, PermissionGroup, User,
    RBAC, DEFAULT_RBAC, TenantStore, get_tenant_store,
)
from agent_system.core.auth.jwt import (
    AuthService, AuthMiddleware, TokenPayload,
    require_auth, user_can,
    set_current_user, reset_current_user, get_current_user,
    get_auth_service,
)

__all__ = [
    # Context
    "TenantContext", "_UserStub", "CrossTenantAccessError",
    "set_tenant_context", "reset_tenant_context", "get_current_tenant", "tenant_scope",
    # Models
    "GlobalRole", "Permission", "Tenant", "Group", "PermissionGroup", "User",
    "RBAC", "DEFAULT_RBAC", "TenantStore", "get_tenant_store",
    # JWT
    "AuthService", "AuthMiddleware", "TokenPayload",
    "require_auth", "user_can",
    "set_current_user", "reset_current_user", "get_current_user",
    "get_auth_service",
]
