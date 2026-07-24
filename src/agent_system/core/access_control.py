"""
Context Isolation — 6-space AccessControl (ARCHITECTURE.md Ch.11)

6 Spaces:
  - Private       : only the owner
  - Perm Group    : shared within a permission group
  - Group         : shared within a group
  - Project       : cross-group temporary project
  - External      : shared with customers/vendors
  - Tenant Public : visible to the entire tenant

Tenant isolation is hard (enforced at the lowest level).
"""

from enum import Enum
from typing import Any, Dict, List, Optional, Set

from pydantic import BaseModel, Field


class SpaceVisibility(str, Enum):
    """6 种空间可见性"""
    PRIVATE = "private"
    PERM_GROUP = "perm_group"
    GROUP = "group"
    PROJECT = "project"
    EXTERNAL = "external"
    TENANT_PUBLIC = "tenant_public"


class Resource(BaseModel):
    """A resource under access control"""
    id: str
    type: str  # task / output / node / tool / ...
    tenant_id: str = "default"
    owner_id: str = ""
    visibility: SpaceVisibility = SpaceVisibility.PRIVATE
    perm_group_ids: list[str] = Field(default_factory=list)
    group_ids: list[str] = Field(default_factory=list)
    project_ids: list[str] = Field(default_factory=list)
    shared_with: list[str] = Field(default_factory=list)  # direct user ids
    metadata: dict[str, Any] = Field(default_factory=dict)


class UserContext(BaseModel):
    """User identity and permissions"""
    user_id: str
    tenant_id: str = "default"
    global_role: str = "user"  # user / admin / platform_admin
    perm_group_ids: list[str] = Field(default_factory=list)
    group_ids: list[str] = Field(default_factory=list)
    project_ids: list[str] = Field(default_factory=list)
    is_agent: bool = False

    @classmethod
    def system(cls) -> "UserContext":
        """System-level agent context"""
        return cls(
            user_id="system",
            tenant_id="default",
            global_role="platform_admin",
            is_agent=True,
        )

    @classmethod
    def agent(cls, agent_name: str, tenant_id: str = "default") -> "UserContext":
        """Agent context with limited permissions"""
        return cls(
            user_id=f"agent:{agent_name}",
            tenant_id=tenant_id,
            global_role="user",
            is_agent=True,
        )


class AccessControl:
    """
    Access control for the 6-space system.

    Rules (ARCHITECTURE.md 11.2):
    1. Tenant isolation (hard) — cross-tenant access always denied
    2. Admin — `platform_admin` (all tenants) or `tenant_admin`
       (within own tenant) get full read/write access
    3. Owner — full access to own resources
    4. Tenant Public — visible to all in the same tenant
    5. Explicit sharing — users in shared_with list
    6. Space-level — check perm_group / group / project membership
    """

    def __init__(self):
        self._resource_store: dict[str, Resource] = {}

    def register_resource(self, resource: Resource):
        self._resource_store[resource.id] = resource

    def get_resource(self, resource_id: str) -> Resource | None:
        return self._resource_store.get(resource_id)

    def can_read(self, user: UserContext, resource: Resource) -> bool:
        """Check if a user can read a resource"""
        return self._check_access(user, resource, "read")

    def can_write(self, user: UserContext, resource: Resource) -> bool:
        """Check if a user can write/modify a resource"""
        return self._check_access(user, resource, "write")

    def can_delete(self, user: UserContext, resource: Resource) -> bool:
        """Check if a user can delete a resource"""
        # Delete requires owner or platform_admin
        if self._is_platform_admin(user):
            return True
        return resource.owner_id == user.user_id

    def _check_access(self, user: UserContext, resource: Resource, access_type: str) -> bool:
        """Core access check logic"""

        # Rule 1: Tenant isolation (hard) — platform_admin is the only
        # exception (sees everything); tenant_admin still scoped to
        # their own tenant.
        if (
            resource.tenant_id != user.tenant_id
            and user.global_role != "platform_admin"
        ):
            return False

        # Rule 2: Admin (platform or tenant).
        if user.global_role in ("platform_admin", "tenant_admin"):
            return True

        # Rule 3: Owner — full access to own resources
        if resource.owner_id == user.user_id:
            return True

        # Write access beyond owner requires careful checks
        if access_type == "write":
            return self._check_write_access(user, resource)

        # Rule 4: Tenant Public — visible to all in same tenant
        if resource.visibility == SpaceVisibility.TENANT_PUBLIC:
            return True

        # Rule 5: Explicit sharing
        if user.user_id in resource.shared_with:
            return True

        # Rule 6: Space-level checks
        return self._check_space_level(user, resource)

    def _check_write_access(self, user: UserContext, resource: Resource) -> bool:
        """Write access is more restrictive"""
        # Admin can write
        if user.global_role in ("admin", "platform_admin"):
            return True
        # Only the owner can write to private resources
        if resource.visibility == SpaceVisibility.PRIVATE:
            return False
        # For shared spaces, check membership
        return self._check_space_level(user, resource)

    def _check_space_level(self, user: UserContext, resource: Resource) -> bool:
        """Check visibility-based access"""
        visibility = resource.visibility

        if visibility == SpaceVisibility.PRIVATE:
            # Only owner (checked above) can access
            return False

        elif visibility == SpaceVisibility.PERM_GROUP:
            return bool(set(user.perm_group_ids) & set(resource.perm_group_ids))

        elif visibility == SpaceVisibility.GROUP:
            return bool(set(user.group_ids) & set(resource.group_ids))

        elif visibility == SpaceVisibility.PROJECT:
            return bool(set(user.project_ids) & set(resource.project_ids))

        elif visibility == SpaceVisibility.EXTERNAL:
            # External requires explicit sharing
            return user.user_id in resource.shared_with

        return False

    def _is_platform_admin(self, user: UserContext) -> bool:
        return user.global_role == "platform_admin"

    def share_with_user(self, resource_id: str, user_id: str) -> bool:
        """Explicitly share a resource with a user"""
        resource = self._resource_store.get(resource_id)
        if not resource:
            return False
        if user_id not in resource.shared_with:
            resource.shared_with.append(user_id)
        return True

    def unshare_with_user(self, resource_id: str, user_id: str) -> bool:
        """Remove a user from shared access"""
        resource = self._resource_store.get(resource_id)
        if not resource:
            return False
        if user_id in resource.shared_with:
            resource.shared_with.remove(user_id)
        return True

    def filter_accessible(
        self,
        user: UserContext,
        resources: list[Resource],
        access_type: str = "read",
    ) -> list[Resource]:
        """Filter a list of resources to only accessible ones"""
        if access_type == "read":
            return [r for r in resources if self.can_read(user, r)]
        else:
            return [r for r in resources if self.can_write(user, r)]

    def can_read_resource_id(self, user: UserContext, resource_id: str) -> bool:
        """Convenience: check read access by resource ID"""
        resource = self._resource_store.get(resource_id)
        if not resource:
            return False
        return self.can_read(user, resource)

    def require_access(
        self,
        user: UserContext,
        resource: Resource,
        access_type: str = "read",
    ):
        """Raise PermissionError if access is denied"""
        check_fn = self.can_read if access_type == "read" else self.can_write
        if not check_fn(user, resource):
            raise PermissionError(
                f"User {user.user_id} (tenant={user.tenant_id}) "
                f"cannot {access_type} resource {resource.id} "
                f"(visibility={resource.visibility.value})"
            )


# Global access control instance
access_control = AccessControl()


def require_space(
    visibility: SpaceVisibility,
    perm_group_ids: list[str] | None = None,
):
    """Decorator: mark a resource with required space visibility"""
    def decorator(func):
        func._required_visibility = visibility
        func._perm_groups = perm_group_ids or []
        return func
    return decorator


class SpaceContext(BaseModel):
    """Execution context for an agent within a space"""
    user: UserContext = Field(default_factory=UserContext.system)
    resource: Resource | None = None
    visibility: SpaceVisibility = SpaceVisibility.PRIVATE

    def assert_read(self):
        if self.resource:
            access_control.require_access(self.user, self.resource, "read")

    def assert_write(self):
        if self.resource:
            access_control.require_access(self.user, self.resource, "write")
