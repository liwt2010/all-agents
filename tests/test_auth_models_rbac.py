"""
Tests: User/Tenant/Group/Permission models + RBAC matrix
"""

import pytest

from agent_system.core.auth.models import (
    GlobalRole,
    Permission,
    Tenant,
    Group,
    PermissionGroup,
    User,
    RBAC,
    DEFAULT_RBAC,
    TenantStore,
    get_tenant_store,
)


class TestTenant:
    def test_create(self):
        t = Tenant(id="acme", name="ACME Inc")
        assert t.id == "acme"
        assert t.status == "active"
        assert t.plan == "free"


class TestGroup:
    def test_basic(self):
        g = Group(id="g1", tenant_id="acme", name="Engineering")
        assert g.tenant_id == "acme"
        assert g.group_type == "project"  # default

    def test_hierarchy(self):
        g = Group(id="g2", tenant_id="acme", name="Sub-team", parent_group_id="g1")
        assert g.parent_group_id == "g1"


class TestPermissionGroup:
    def test_with_permissions(self):
        pg = PermissionGroup(
            id="pg1",
            tenant_id="acme",
            name="admins",
            role=GlobalRole.GROUP_ADMIN,
            permissions=[Permission.READ, Permission.WRITE, Permission.ADMIN],
        )
        assert pg.role == GlobalRole.GROUP_ADMIN
        assert len(pg.permissions) == 3


class TestUser:
    def test_basic(self):
        u = User(id="u1", tenant_id="acme", email="alice@acme.com", global_role=GlobalRole.USER)
        assert u.global_role == GlobalRole.USER
        assert u.is_agent is False

    def test_agent_user(self):
        u = User(id="agent:tech", tenant_id="acme", is_agent=True)
        assert u.is_agent is True


class TestRBAC:
    def test_role_can(self):
        rbac = RBAC()
        assert rbac.role_can(GlobalRole.PLATFORM_ADMIN, Permission.AUDIT) is True
        assert rbac.role_can(GlobalRole.VIEWER, Permission.READ) is True
        assert rbac.role_can(GlobalRole.VIEWER, Permission.WRITE) is False
        assert rbac.role_can(GlobalRole.USER, Permission.EXPORT) is False

    def test_user_permissions_aggregate(self):
        rbac = RBAC()
        # Create a perm group with extra permissions
        store = get_tenant_store()
        pg = PermissionGroup(
            id="pg-special",
            tenant_id="acme",
            name="exporters",
            role=GlobalRole.USER,
            permissions=[Permission.EXPORT],
        )
        store.create_perm_group(pg)

        user = User(
            id="u1", tenant_id="acme", global_role=GlobalRole.USER,
            perm_group_ids=["pg-special"],
        )

        # User inherits role perms + group perms
        perms = rbac.user_permissions(user)
        assert Permission.READ in perms
        assert Permission.WRITE in perms
        assert Permission.EXPORT in perms  # from perm group
        assert Permission.AUDIT not in perms  # not in either

    def test_user_can(self):
        rbac = RBAC()
        user = User(id="u1", tenant_id="acme", global_role=GlobalRole.GROUP_ADMIN)
        assert rbac.user_can(user, Permission.INVITE) is True
        assert rbac.user_can(user, Permission.AUDIT) is False  # not in GROUP_ADMIN

    def test_audit_log_requires_audit_perm(self):
        rbac = RBAC()
        user = User(id="u1", tenant_id="acme", global_role=GlobalRole.USER)
        # USER can read tasks
        assert rbac.user_can_on_resource(user, Permission.READ, "task") is True
        # But cannot read audit_log (needs AUDIT permission)
        assert rbac.user_can_on_resource(user, Permission.READ, "audit_log") is False

    def test_tenant_admin_can_audit(self):
        rbac = RBAC()
        admin = User(id="a1", tenant_id="acme", global_role=GlobalRole.TENANT_ADMIN)
        assert rbac.user_can_on_resource(admin, Permission.READ, "audit_log") is True

    def test_custom_matrix(self):
        custom = {GlobalRole.USER: {Permission.READ}}
        rbac = RBAC(custom_matrix=custom)
        assert rbac.role_can(GlobalRole.USER, Permission.WRITE) is False
        # Custom matrix replaces default; other roles only have what's in custom
        assert rbac.role_can(GlobalRole.PLATFORM_ADMIN, Permission.WRITE) is False
        assert rbac.role_can(GlobalRole.PLATFORM_ADMIN, Permission.READ) is False
        # Empty result for missing role
        assert rbac.role_can(GlobalRole.PLATFORM_ADMIN, Permission.AUDIT) is False


class TestTenantStore:
    def test_round_trip(self):
        store = TenantStore()
        tenant = Tenant(id="acme", name="ACME")
        store.create_tenant(tenant)
        assert store.get_tenant("acme").name == "ACME"

        user = User(id="u1", tenant_id="acme", email="alice@acme.com")
        store.create_user(user)
        assert store.get_user("u1").email == "alice@acme.com"

        # List users by tenant
        acme_users = store.list_users(tenant_id="acme")
        assert len(acme_users) == 1
        beta_users = store.list_users(tenant_id="beta")
        assert len(beta_users) == 0

    def test_groups_in_tenant(self):
        store = TenantStore()
        g1 = Group(id="g1", tenant_id="acme", name="Eng")
        g2 = Group(id="g2", tenant_id="beta", name="Eng")
        store.create_group(g1)
        store.create_group(g2)
        acme_groups = store.list_groups(tenant_id="acme")
        assert len(acme_groups) == 1
        assert acme_groups[0].id == "g1"

    def test_perm_groups(self):
        store = TenantStore()
        pg = PermissionGroup(
            id="pg1", tenant_id="acme", name="admins",
            role=GlobalRole.GROUP_ADMIN, permissions=[Permission.ADMIN],
        )
        store.create_perm_group(pg)
        assert store.get_perm_group("pg1").role == GlobalRole.GROUP_ADMIN

    def test_tenant_isolation(self):
        store = TenantStore()
        store.create_user(User(id="u1", tenant_id="acme"))
        store.create_user(User(id="u2", tenant_id="beta"))
        # Even with a typo, tenant_id check is the key
        assert len(store.list_users(tenant_id="acme")) == 1
        assert len(store.list_users(tenant_id="beta")) == 1


class TestRBACFullScenario:
    """End-to-end: 4 users, 3 roles, 2 perm groups, verify permissions."""

    def test_scenario(self):
        store = TenantStore()
        # Set up tenant
        store.create_tenant(Tenant(id="acme", name="ACME"))

        # Perm groups: admins (full) and viewers (read-only)
        store.create_perm_group(PermissionGroup(
            id="pg-admins", tenant_id="acme", name="admins",
            role=GlobalRole.GROUP_ADMIN,
            permissions=[Permission.READ, Permission.WRITE, Permission.DELETE],
        ))
        store.create_perm_group(PermissionGroup(
            id="pg-viewers", tenant_id="acme", name="viewers",
            role=GlobalRole.VIEWER,
            permissions=[],
        ))

        # Users
        admin = User(
            id="alice", tenant_id="acme", global_role=GlobalRole.GROUP_ADMIN,
            perm_group_ids=["pg-admins"],
        )
        viewer = User(
            id="bob", tenant_id="acme", global_role=GlobalRole.VIEWER,
            perm_group_ids=["pg-viewers"],
        )
        regular = User(
            id="carol", tenant_id="acme", global_role=GlobalRole.USER,
        )

        rbac = RBAC()

        # Alice: GROUP_ADMIN + pg-admins
        assert rbac.user_can(admin, Permission.WRITE) is True
        assert rbac.user_can(admin, Permission.DELETE) is True
        assert rbac.user_can(admin, Permission.AUDIT) is False  # not granted

        # Bob: VIEWER + pg-viewers (no extra perms)
        assert rbac.user_can(viewer, Permission.READ) is True
        assert rbac.user_can(viewer, Permission.WRITE) is False
        assert rbac.user_can(viewer, Permission.DELETE) is False

        # Carol: USER only
        assert rbac.user_can(regular, Permission.READ) is True
        assert rbac.user_can(regular, Permission.WRITE) is True
        assert rbac.user_can(regular, Permission.DELETE) is False
        assert rbac.user_can(regular, Permission.EXPORT) is False


class TestGlobalStore:
    def test_singleton(self):
        s1 = get_tenant_store()
        s2 = get_tenant_store()
        assert s1 is s2
