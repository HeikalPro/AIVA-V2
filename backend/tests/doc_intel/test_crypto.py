"""Credential encryption (backend.doc_intel.crypto): Fernet with MultiFernet rotation, fail
closed, and no key material or plaintext in any exception, repr or CLI output.
No database, no network; every key and secret here is generated or obviously fake.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import traceback
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from backend.doc_intel import crypto
from backend.doc_intel.crypto import (
    CIPHERTEXT_PREFIX,
    SecretBox,
    SecretsUnavailable,
    generate_key,
    secret_hint,
    secrets_configured,
)
from backend.doc_intel.settings import DocIntelSettings

ROOT = Path(__file__).resolve().parents[3]
PLAINTEXT = "fake-client-secret~0000.for-tests"
ARABIC = "كلمة سر تجريبية"
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{43}=$")


@pytest.fixture(autouse=True)
def _no_key_in_the_environment(monkeypatch):
    monkeypatch.delenv("DOC_INTEL_SECRETS_KEY", raising=False)


def settings_with(value: str | None) -> DocIntelSettings:
    return DocIntelSettings(_env_file=None, secrets_key=value)


def box(*keys: str) -> SecretBox:
    return SecretBox.from_settings(settings_with(",".join(keys)))


def everything_about(exc: BaseException) -> str:
    """Every way an exception can end up in a log, a response or a crash report."""
    return "\n".join([
        str(exc), repr(exc), repr(exc.args), str(getattr(exc, "reason", "")),
        "".join(traceback.format_exception(exc)),
    ])


# ---- round trip, format and rotation --------------------------------------------------------


@pytest.mark.parametrize("plaintext", [PLAINTEXT, ARABIC, "", "x" * 2048])
def test_round_trip(plaintext):
    key = generate_key()
    secret_box = box(key)
    ciphertext = secret_box.encrypt(plaintext)
    assert ciphertext.startswith(CIPHERTEXT_PREFIX) and CIPHERTEXT_PREFIX == "fernet:v1:"
    assert plaintext == "" or plaintext not in ciphertext
    assert secret_box.decrypt(ciphertext) == plaintext
    # The stored token is a plain Fernet token under the configured key.
    token = ciphertext[len(CIPHERTEXT_PREFIX):]
    assert Fernet(key).decrypt(token.encode("ascii")).decode("utf-8") == plaintext


def test_every_encryption_is_fresh():
    secret_box = box(generate_key())
    first, second = secret_box.encrypt(PLAINTEXT), secret_box.encrypt(PLAINTEXT)
    assert first != second  # random IV and timestamp
    assert secret_box.decrypt(first) == secret_box.decrypt(second) == PLAINTEXT


def test_rotation_new_key_encrypts_old_key_still_decrypts():
    old, new = generate_key(), generate_key()
    stored_with_old = box(old).encrypt(PLAINTEXT)

    rotated = box(new, old)
    assert rotated.key_count == 2
    assert rotated.decrypt(stored_with_old) == PLAINTEXT

    stored_with_new = rotated.encrypt(ARABIC)
    assert box(new).decrypt(stored_with_new) == ARABIC  # the FIRST key encrypted it
    with pytest.raises(SecretsUnavailable) as info:
        box(old).decrypt(stored_with_new)
    assert info.value.code == "decrypt_failed"


def test_keys_are_trimmed_and_blank_entries_ignored():
    key = generate_key()
    ciphertext = box(key).encrypt(PLAINTEXT)
    assert SecretBox.from_settings(settings_with(f"  {key} ,, \n")).decrypt(ciphertext) == PLAINTEXT


def test_the_environment_variable_is_read(monkeypatch):
    key = generate_key()
    monkeypatch.setenv("DOC_INTEL_SECRETS_KEY", key)
    settings = DocIntelSettings(_env_file=None)
    assert secrets_configured(settings)
    assert SecretBox.from_settings(settings).decrypt(box(key).encrypt(PLAINTEXT)) == PLAINTEXT


# ---- failures ---------------------------------------------------------------------------------


def _tampered(ciphertext: str) -> str:
    token = ciphertext[len(CIPHERTEXT_PREFIX):]
    middle = len(token) // 2
    flipped = "A" if token[middle] != "A" else "B"
    return CIPHERTEXT_PREFIX + token[:middle] + flipped + token[middle + 1:]


def test_tampered_or_malformed_ciphertext_fails_closed():
    secret_box = box(generate_key())
    good = secret_box.encrypt(PLAINTEXT)
    bad_values = [
        _tampered(good),
        good[:-6],  # truncated
        good + "AAAA",
        good.replace(CIPHERTEXT_PREFIX, "fernet:v2:"),
        good[len(CIPHERTEXT_PREFIX):],  # the bare token, no prefix
        PLAINTEXT,  # a plaintext value that was never encrypted
        CIPHERTEXT_PREFIX,
        CIPHERTEXT_PREFIX + "ümlaut-not-ascii",
        "",
        None,
        12345,
    ]
    for value in bad_values:
        with pytest.raises(SecretsUnavailable) as info:
            secret_box.decrypt(value)  # type: ignore[arg-type]
        assert info.value.code == "decrypt_failed", value
        assert PLAINTEXT not in everything_about(info.value)
        assert info.value.__cause__ is None


def test_an_unknown_key_fails_closed_with_an_actionable_reason():
    stored = box(generate_key()).encrypt(PLAINTEXT)
    with pytest.raises(SecretsUnavailable) as info:
        box(generate_key()).decrypt(stored)
    assert info.value.code == "decrypt_failed"
    assert "DOC_INTEL_SECRETS_KEY" in info.value.reason
    assert PLAINTEXT not in everything_about(info.value)


@pytest.mark.parametrize("value", [None, "", "   ", " , ,, "])
def test_a_missing_key_raises_key_missing(value):
    settings = settings_with(value)
    with pytest.raises(SecretsUnavailable) as info:
        SecretBox.from_settings(settings)
    assert info.value.code == "key_missing"
    assert "DOC_INTEL_SECRETS_KEY" in info.value.reason
    assert "generate-key" in info.value.reason
    assert secrets_configured(settings) is False


@pytest.mark.parametrize(
    "keys, which",
    [
        (["not-a-fernet-key"], "the key"),
        (["QUJD" * 11], "the key"),  # 44 characters of base64, but 33 bytes
        ([generate_key(), "c2hvcnQta2V5LXZhbHVl"], "key 2 of 2"),
        (["!" * 44, generate_key()], "key 1 of 2"),
    ],
)
def test_an_invalid_key_raises_key_invalid_without_echoing_it(keys, which):
    settings = settings_with(",".join(keys))
    with pytest.raises(SecretsUnavailable) as info:
        SecretBox.from_settings(settings)
    exc = info.value
    assert exc.code == "key_invalid"
    assert "DOC_INTEL_SECRETS_KEY" in exc.reason and which in exc.reason
    dump = everything_about(exc)
    for key in keys:
        assert key not in dump
    assert exc.__cause__ is None
    assert secrets_configured(settings) is False


def test_secrets_configured_never_raises():
    assert secrets_configured(settings_with(generate_key())) is True
    assert secrets_configured(object()) is False  # type: ignore[arg-type]


def test_encrypt_needs_a_string():
    with pytest.raises(TypeError):
        box(generate_key()).encrypt(None)  # type: ignore[arg-type]


def test_no_key_material_in_reprs():
    key = generate_key()
    secret_box = box(key, generate_key())
    assert repr(secret_box) == "SecretBox(keys=2)"
    assert key not in repr(secret_box) and key not in str(secret_box)
    assert key not in repr(settings_with(key))  # SecretStr in the settings


# ---- keys and hints -----------------------------------------------------------------------------


def test_generate_key_makes_distinct_valid_fernet_keys():
    keys = {generate_key() for _ in range(20)}
    assert len(keys) == 20
    for key in keys:
        assert KEY_RE.match(key)
        Fernet(key)  # usable as is


@pytest.mark.parametrize(
    "secret, hint",
    [
        ("", ""),
        ("a", "…"),
        ("abcdefg", "…"),  # 7 characters: fully hidden
        ("abcdefgh", "…efgh"),  # 8 characters: the last 4 are shown
        ("fake~secret.value_for_tests-9Zq4", "…9Zq4"),
        ("سر-تجريبي-طويل-جدا", "…" + "سر-تجريبي-طويل-جدا"[-4:]),
    ],
)
def test_secret_hint(secret, hint):
    assert secret_hint(secret) == hint
    if len(secret) >= 8:
        assert secret[:-4] not in secret_hint(secret)


# ---- CLI ------------------------------------------------------------------------------------------


def test_cli_generate_key_prints_only_a_key():
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("DOC_INTEL_")}
    result = subprocess.run(
        [sys.executable, "-m", "backend.doc_intel.crypto", "generate-key"],
        cwd=str(ROOT), env=env, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == b""
    lines = result.stdout.decode("ascii").splitlines()
    assert len(lines) == 1 and KEY_RE.match(lines[0])
    Fernet(lines[0])


def test_cli_rejects_unknown_or_missing_commands(capsys):
    for argv in ([], ["show-key"]):
        with pytest.raises(SystemExit) as info:
            crypto.main(argv)
        assert info.value.code == 2
    captured = capsys.readouterr()
    assert not KEY_RE.search(captured.out)


def test_cli_main_in_process(capsys):
    assert crypto.main(["generate-key"]) == 0
    out = capsys.readouterr().out
    assert KEY_RE.match(out.strip()) and out.endswith("\n")
