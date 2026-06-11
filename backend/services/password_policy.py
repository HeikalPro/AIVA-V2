from __future__ import annotations

import re

from backend.config import get_settings
from backend.exceptions import BadRequestError

_COMPLEXITY_RULES = [
    (lambda p: re.search(r"[A-Z]", p), "Password must contain an uppercase letter."),
    (lambda p: re.search(r"[a-z]", p), "Password must contain a lowercase letter."),
    (lambda p: re.search(r"\d", p), "Password must contain a number."),
    (lambda p: re.search(r"[^A-Za-z0-9]", p), "Password must contain a special character."),
]


def validate_password(password: str, *, confirm: str | None = None) -> None:
    settings = get_settings()
    if confirm is not None and password != confirm:
        raise BadRequestError("Passwords do not match.")
    min_len = settings.password_min_length
    if len(password) < min_len:
        raise BadRequestError(f"Password must be at least {min_len} characters.")
    if not settings.password_require_complexity:
        return
    for rule, message in _COMPLEXITY_RULES:
        if not rule(password):
            raise BadRequestError(message)
