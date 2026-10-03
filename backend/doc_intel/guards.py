"""Role-only access guards for every document intelligence endpoint.

Deliberately NOT ``require_roles_or_nav_permission``: page permissions can be
granted to any user (including agents) through the per-user page-access editor,
so a page key must never open these APIs. ``require_roles`` lets SUPER_ADMIN
through automatically.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from backend.auth.deps import UserContext, require_roles
from backend.auth.role_constants import ROLE_DEVELOPER, ROLE_SUPER_ADMIN

# Admin = Super Admin only (decision D1).
admin_only = require_roles(ROLE_SUPER_ADMIN)
# Monitoring, logs and diagnostics: Super Admin + Developer.
admin_or_developer = require_roles(ROLE_DEVELOPER)

AdminUser = Annotated[UserContext, Depends(admin_only)]
MonitorUser = Annotated[UserContext, Depends(admin_or_developer)]
