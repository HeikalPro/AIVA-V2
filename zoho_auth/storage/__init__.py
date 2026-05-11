"""Persistence layer for Zoho sessions.

The default ``JsonFileTokenStore`` writes to a JSON file in the user's home
directory. Implement the ``TokenStore`` protocol to swap in a database, an OS
keyring, an encrypted vault, or anything else.
"""

from .token_store import (
    NullTokenStore,
    StoredSession,
    TokenStore,
)
from .file_token_store import JsonFileTokenStore

__all__ = [
    "JsonFileTokenStore",
    "NullTokenStore",
    "StoredSession",
    "TokenStore",
]
