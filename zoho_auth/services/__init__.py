"""Service layer.

Each module in this package contains a single ``Service`` subclass with one
clear responsibility. To add a new service:

1. Create ``zoho_auth/services/<name>_service.py``.
2. Subclass ``Service`` and inject collaborators through ``__init__``.
3. Add a ``build_<name>_service()`` method on ``ServiceContainer``.
4. Construct the new service in ``ServiceContainer.__init__`` so it is wired
   up automatically alongside the existing ones.
"""

from .auth_service import AuthService
from .profile_service import ProfileService
from .token_service import TokenService

__all__ = ["AuthService", "ProfileService", "TokenService"]
