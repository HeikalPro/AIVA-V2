"""Encryption of stored integration credentials (the Microsoft tenant id, client id and secret).

Fernet (AES-128-CBC + HMAC-SHA256, the ``cryptography`` package) with MultiFernet key
rotation. Keys come ONLY from ``DOC_INTEL_SECRETS_KEY``: comma separated, the first key
encrypts and every key decrypts. Ciphertexts are stored as ``fernet:v1:<token>``.

Fail closed: a missing or invalid key raises SecretsUnavailable, never a silent fallback.
No exception, repr or log line produced here contains key material or plaintext.

Rotating the key:
1. put the new key FIRST and keep the old one after it
   (``DOC_INTEL_SECRETS_KEY=<new>,<old>``) and restart;
2. re-save every source's credentials: paste its client secret again (or a new one) and save.
   Any credential save re-encrypts ALL of the source's stored credentials with the new key,
   the unchanged Tenant and Client IDs included (``SyncService.update_source``);
3. then drop the old key.

CLI:  python -m backend.doc_intel.crypto generate-key
"""
from __future__ import annotations

import argparse
import sys

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from backend.doc_intel.settings import DocIntelSettings

CIPHERTEXT_PREFIX = "fernet:v1:"
SECRETS_KEY_ENV = "DOC_INTEL_SECRETS_KEY"

_GENERATE_COMMAND = "python -m backend.doc_intel.crypto generate-key"
_HINT_MIN_LENGTH = 8
_HINT_CHARS = 4
_HINT_PREFIX = "…"

_KEY_MISSING_REASON = (
    f"Credential encryption is not configured: set {SECRETS_KEY_ENV} (generate a key with "
    f"`{_GENERATE_COMMAND}`) in the backend .env and restart the backend"
)
_BAD_FORMAT_REASON = "A stored credential is not in the expected encrypted format — enter it again"
_DECRYPT_FAILED_REASON = (
    f"A stored credential could not be decrypted: it was encrypted with a key that is not in "
    f"{SECRETS_KEY_ENV}, or it was modified. Add the previous key after the current one "
    "(comma separated) and restart, or enter the credential again"
)


class SecretsUnavailable(Exception):
    """Credentials cannot be encrypted/decrypted; ``reason`` is shown to admins as-is
    (it never contains key material or plaintext)."""

    def __init__(self, reason: str, *, code: str = "secrets_unavailable") -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code  # key_missing | key_invalid | decrypt_failed | encrypt_failed


class SecretBox:
    """Encrypts and decrypts credential strings with the configured Fernet key(s).

    Build it with ``SecretBox.from_settings``. Its repr shows only how many keys it holds.
    """

    __slots__ = ("_fernet", "_key_count")

    def __init__(self, keys: list[Fernet]) -> None:
        if not keys:
            raise SecretsUnavailable(_KEY_MISSING_REASON, code="key_missing")
        self._fernet = MultiFernet(keys)
        self._key_count = len(keys)

    @classmethod
    def from_settings(cls, settings: DocIntelSettings) -> "SecretBox":
        """Raises SecretsUnavailable(code="key_missing"|"key_invalid")."""
        raw = settings.secrets_key.get_secret_value() if settings.secrets_key is not None else ""
        parts = [part.strip() for part in (raw or "").split(",")]
        keys = [part for part in parts if part]
        if not keys:
            raise SecretsUnavailable(_KEY_MISSING_REASON, code="key_missing")
        fernets: list[Fernet] = []
        for index, key in enumerate(keys, start=1):
            try:
                fernets.append(Fernet(key))
            except (ValueError, TypeError):  # binascii.Error is a ValueError
                # ``from None``: the library's message is generic, but the key must never ride along.
                which = f"key {index} of {len(keys)}" if len(keys) > 1 else "the key"
                raise SecretsUnavailable(
                    f"{SECRETS_KEY_ENV} is invalid: {which} is not a Fernet key (44 characters of "
                    f"url-safe base64). Generate one with `{_GENERATE_COMMAND}` and restart the backend",
                    code="key_invalid",
                ) from None
        return cls(fernets)

    @property
    def key_count(self) -> int:
        return self._key_count

    def encrypt(self, plaintext: str) -> str:
        """``fernet:v1:<token>``. Raises SecretsUnavailable on failure."""
        if not isinstance(plaintext, str):
            raise TypeError("plaintext must be a str")
        try:
            token = self._fernet.encrypt(plaintext.encode("utf-8"))
        except Exception:
            raise SecretsUnavailable("The credential could not be encrypted", code="encrypt_failed") from None
        return CIPHERTEXT_PREFIX + token.decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        """Raises SecretsUnavailable(code="decrypt_failed") for tampered text or an unknown key."""
        if not isinstance(ciphertext, str) or not ciphertext.startswith(CIPHERTEXT_PREFIX):
            raise SecretsUnavailable(_BAD_FORMAT_REASON, code="decrypt_failed")
        token = ciphertext[len(CIPHERTEXT_PREFIX):]
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, TypeError):  # UnicodeError is a ValueError
            raise SecretsUnavailable(_DECRYPT_FAILED_REASON, code="decrypt_failed") from None

    def __repr__(self) -> str:
        return f"SecretBox(keys={self._key_count})"


def secrets_configured(settings: DocIntelSettings) -> bool:
    """True when DOC_INTEL_SECRETS_KEY holds at least one valid Fernet key. Never raises."""
    try:
        SecretBox.from_settings(settings)
    except Exception:
        return False
    return True


def generate_key() -> str:
    """A new random Fernet key (url-safe base64, 44 chars)."""
    return Fernet.generate_key().decode("ascii")


def secret_hint(secret: str) -> str:
    """Display hint for a stored secret: "…" + its last 4 characters (all of it hidden when shorter than 8)."""
    if not secret:
        return ""
    if len(secret) < _HINT_MIN_LENGTH:
        return _HINT_PREFIX
    return _HINT_PREFIX + secret[-_HINT_CHARS:]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.doc_intel.crypto",
        description="Credential encryption key tools for the document intelligence module.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "generate-key",
        help=f"print a new Fernet key for {SECRETS_KEY_ENV} (keep a copy in the team password manager)",
    )
    parser.parse_args(argv)  # "generate-key" is the only command (argparse rejects anything else)
    sys.stdout.write(generate_key() + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
