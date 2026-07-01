"""
User / Tenant / Group / Permission models — PLATFORM §7.7, §28

This module extends the existing access_control primitives with the
explicit multi-tenant hierarchy:

  Platform
    └─ Tenant (e.g. company)
         └─ Group (e.g. project / department)
              └─ PermissionGroup (e.g. admins / members / viewers)
                   └─ User

Plus an RBAC matrix mapping roles to permissions per resource type.
"""

import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ── Role definitions (RBAC matrix) ──

class GlobalRole(str, Enum):
    """Roles at platform / tenant level."""
    PLATFORM_ADMIN = "platform_admin"   # cross-tenant superuser
    TENANT_ADMIN = "tenant_admin"       # tenant superuser
    GROUP_ADMIN = "group_admin"         # group superuser
    USER = "user"                       # regular user
    AGENT = "agent"                     # non-human agent
    VIEWER = "viewer"                   # read-only


class Permission(str, Enum):
    """Standard permission actions."""
    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    ADMIN = "admin"     # manage others (invite, role change)
    AUDIT = "audit"     # view audit log
    EXPORT = "export"   # export tenant data
    INVITE = "invite"   # invite new users


# ── Tenant ──

class Tenant(BaseModel):
    """A tenant = one company / organization."""
    id: str
    name: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    plan: str = "free"             # free / pro / enterprise
    status: str = "active"         # active / suspended / trial
    isolation_mode: str = "schema"  # schema (logical) / db (physical)
    metadata: Dict[str, Any] = Field(default_factory=dict)


# ── Group (project / department) ──

class Group(BaseModel):
    """A group within a tenant (e.g. a project, a department)."""
    id: str
    tenant_id: str
    name: str
    group_type: str = "project"  # project / department / custom
    parent_group_id: Optional[str] = None  # for hierarchy
    visibility: str = "private"  # private / public-within-tenant
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: Dict[str, Any] = Field(default_factory=dict)


# ── PermissionGroup (admin / member / viewer) ──

class PermissionGroup(BaseModel):
    """A role-based permission group within a group/tenant."""
    id: str
    tenant_id: str
    group_id: Optional[str] = None   # which group this is scoped to (None = tenant-wide)
    name: str                       # "admins" / "members" / "viewers"
    role: GlobalRole = GlobalRole.USER
    permissions: List[Permission] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ── User (extended from existing access_control.UserContext) ──

class User(BaseModel):
    """A user within a tenant."""
    id: str
    tenant_id: str
    email: str = ""
    display_name: str = ""
    global_role: GlobalRole = GlobalRole.USER
    group_ids: List[str] = Field(default_factory=list)
    perm_group_ids: List[str] = Field(default_factory=list)
    project_ids: List[str] = Field(default_factory=list)
    is_agent: bool = False
    status: str = "active"  # active / invited / suspended / disabled
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_active_at: Optional[datetime] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


# ── RBAC matrix ──

# Maps (global_role, resource_action) -> allowed
# This is the default matrix; tenants can override via PermissionGroup.
DEFAULT_RBAC: Dict[GlobalRole, Set[Permission]] = {
    GlobalRole.PLATFORM_ADMIN: {
        Permission.READ, Permission.WRITE, Permission.DELETE,
        Permission.ADMIN, Permission.AUDIT, Permission.EXPORT, Permission.INVITE,
    },
    GlobalRole.TENANT_ADMIN: {
        Permission.READ, Permission.WRITE, Permission.DELETE,
        Permission.ADMIN, Permission.AUDIT, Permission.EXPORT, Permission.INVITE,
    },
    GlobalRole.GROUP_ADMIN: {
        Permission.READ, Permission.WRITE, Permission.DELETE,
        Permission.ADMIN, Permission.INVITE,
    },
    GlobalRole.USER: {
        Permission.READ, Permission.WRITE, Permission.INVITE,
    },
    GlobalRole.VIEWER: {
        Permission.READ,
    },
    GlobalRole.AGENT: {
        Permission.READ, Permission.WRITE,  # limited by tenant/group scope
    },
}


class RBAC:
    """
    Role-Based Access Control.

    Combines the global role (from User) with the permission group
    membership (PermissionGroup) to determine what actions a user can
    take on a given resource type.
    """

    def __init__(self, custom_matrix: Optional[Dict[GlobalRole, Set[Permission]]] = None):
        self.matrix = custom_matrix or {k: set(v) for k, v in DEFAULT_RBAC.items()}

    def role_can(self, role: GlobalRole, permission: Permission) -> bool:
        """Check if a role has a permission"""
        return permission in self.matrix.get(role, set())

    def user_permissions(self, user: User) -> Set[Permission]:
        """Aggregate permissions from user's role and perm groups."""
        perms = set(self.matrix.get(user.global_role, set()))

        # Permission groups add extra permissions (not replace)
        for pg_id in user.perm_group_ids:
            pg = _perm_group_store.get(pg_id)
            if pg:
                perms.update(pg.permissions)
        return perms

    def user_can(self, user: User, permission: Permission) -> bool:
        """Check if a user has a permission through their role + groups."""
        return permission in self.user_permissions(user)

    def user_can_on_resource(
        self,
        user: User,
        permission: Permission,
        resource_type: str = "task",
    ) -> bool:
        """
        Per-resource-type RBAC check.

        Some resources (e.g. audit_log) require a higher role regardless
        of perm groups. This is a hook for that.
        """
        # Audit log always requires AUDIT permission
        if resource_type == "audit_log" and permission == Permission.READ:
            return self.user_can(user, Permission.AUDIT)
        return self.user_can(user, permission)


# In-memory stores (placeholder; production = Postgres / SQL)
_tenant_store: Dict[str, Tenant] = {}
_user_store: Dict[str, User] = {}
_group_store: Dict[str, Group] = {}
_perm_group_store: Dict[str, PermissionGroup] = {}


class TenantStore:
    """In-memory CRUD for tenants, users, groups, permission groups."""

    def __init__(self):
        self.tenants: Dict[str, Tenant] = {}
        self.users: Dict[str, User] = {}
        self.groups: Dict[str, Group] = {}
        self.perm_groups: Dict[str, PermissionGroup] = {}

    # ── Tenants ──
    def create_tenant(self, tenant: Tenant) -> Tenant:
        self.tenants[tenant.id] = tenant
        return tenant

    def get_tenant(self, tenant_id: str) -> Optional[Tenant]:
        return self.tenants.get(tenant_id)

    def list_tenants(self) -> List[Tenant]:
        return list(self.tenants.values())

    # ── Users ──
    def create_user(self, user: User) -> User:
        self.users[user.id] = user
        return user

    def get_user(self, user_id: str) -> Optional[User]:
        return self.users.get(user_id)

    def list_users(self, tenant_id: Optional[str] = None) -> List[User]:
        users = list(self.users.values())
        if tenant_id:
            users = [u for u in users if u.tenant_id == tenant_id]
        return users

    # ── Groups ──
    def create_group(self, group: Group) -> Group:
        self.groups[group.id] = group
        return group

    def get_group(self, group_id: str) -> Optional[Group]:
        return self.groups.get(group_id)

    def list_groups(self, tenant_id: Optional[str] = None) -> List[Group]:
        groups = list(self.groups.values())
        if tenant_id:
            groups = [g for g in groups if g.tenant_id == tenant_id]
        return groups

    # ── Permission Groups ──
    def create_perm_group(self, pg: PermissionGroup) -> PermissionGroup:
        self.perm_groups[pg.id] = pg
        return pg

    def get_perm_group(self, pg_id: str) -> Optional[PermissionGroup]:
        return self.perm_groups.get(pg_id)

    def list_perm_groups(self, tenant_id: Optional[str] = None) -> List[PermissionGroup]:
        pgs = list(self.perm_groups.values())
        if tenant_id:
            pgs = [p for p in pgs if p.tenant_id == tenant_id]
        return pgs


# Default global store
_default_store: Optional[TenantStore] = None


def get_tenant_store() -> TenantStore:
    global _default_store
    if _default_store is None:
        _default_store = TenantStore()
        # Wire up the module-level dicts to the same store
        _default_store.tenants = _tenant_store
        _default_store.users = _user_store
        _default_store.groups = _group_store
        _default_store.perm_groups = _perm_group_store
    return _default_store
