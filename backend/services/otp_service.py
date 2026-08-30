from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Request

from backend.auth.deps import ROLE_AGENT
from backend.auth.hashing import hash_password
from backend.auth.status import (
    OTP_PURPOSE_FORGOT_PASSWORD,
    OTP_PURPOSE_SIGNUP,
    STATUS_ACTIVE,
    STATUS_PENDING_EMAIL_VERIFICATION,
)
from backend.config import Settings, get_settings
from backend.database import Database
from backend.exceptions import BadRequestError, UnauthorizedError
from backend.services.auth_audit import log_auth_event
from backend.services.email import get_mail_sender
from backend.services.email.templates import build_message
from backend.services.password_policy import validate_password

_log = logging.getLogger(__name__)


def _hash_otp(otp: str) -> str:
    return hashlib.sha256(otp.encode("utf-8")).hexdigest()


def _generate_otp(length: int) -> str:
    return "".join(secrets.choice("0123456789") for _ in range(length))


def _split_name(name: str) -> tuple[str, str]:
    parts = name.strip().split(None, 1)
    if not parts:
        return "User", ""
    first = parts[0]
    last = parts[1] if len(parts) > 1 else ""
    return first, last


async def _resolve_signup_organization_id(db: Database, settings: Settings) -> int:
    if settings.signup_default_organization_id is not None:
        org = await db.fetch_one(
            "SELECT id FROM AIVA_organizations WHERE id = :id AND status = 'ACTIVE'",
            {"id": settings.signup_default_organization_id},
        )
        if not org:
            raise BadRequestError("Signup organization is not configured")
        return int(org["id"])

    org = await db.fetch_one(
        "SELECT id FROM AIVA_organizations WHERE code = :code AND status = 'ACTIVE'",
        {"code": settings.bootstrap_superadmin_organization_code},
    )
    if not org:
        raise BadRequestError("No organization available for signup")
    return int(org["id"])


async def _resolve_signup_role_id(db: Database, settings: Settings) -> int:
    role_name = settings.signup_default_role or ROLE_AGENT
    role = await db.fetch_one(
        "SELECT id FROM AIVA_roles WHERE name = :name",
        {"name": role_name},
    )
    if not role:
        raise BadRequestError(f"Signup role '{role_name}' is not configured")
    return int(role["id"])


async def _invalidate_unused_otps(db: Database, user_id: int, purpose: str) -> None:
    await db.execute(
        """
        UPDATE AIVA_email_otps
        SET used_at = SYSTIMESTAMP
        WHERE user_id = :user_id AND purpose = :purpose AND used_at IS NULL
        """,
        {"user_id": user_id, "purpose": purpose},
    )


async def _create_and_send_otp(
    db: Database,
    *,
    user_id: int,
    email: str,
    purpose: str,
    request: Request | None,
    resend_count: int = 0,
) -> None:
    settings = get_settings()
    otp = _generate_otp(settings.otp_length)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.otp_expiry_minutes)

    await db.execute(
        """
        INSERT INTO AIVA_email_otps (
            user_id, purpose, otp_hash, expires_at, resend_count, last_sent_at
        ) VALUES (
            :user_id, :purpose, :otp_hash, :expires_at, :resend_count, SYSTIMESTAMP
        )
        """,
        {
            "user_id": user_id,
            "purpose": purpose,
            "otp_hash": _hash_otp(otp),
            "expires_at": expires_at,
            "resend_count": resend_count,
        },
    )

    app_name = settings.app_name
    if purpose == OTP_PURPOSE_SIGNUP:
        subject = f"{app_name} — Verify your email address"
        title = "Verify your email address"
        intro = (
            "Thank you for registering. Please use the verification code below to "
            "confirm your email address and activate your account."
        )
    else:
        subject = f"{app_name} — Password reset code"
        title = "Reset your password"
        intro = (
            "We received a request to reset the password for your account. "
            "Please use the verification code below to continue."
        )

    minutes = settings.otp_expiry_minutes
    note = (
        f"For your security, this code expires in {minutes} minutes and can be used once. "
        "If you did not request it, no action is required — you may safely ignore this email."
    )
    content = {
        "title": title,
        "intro": intro,
        "callout": otp,
        "callout_label": "Verification code",
        "note": note,
    }
    msg = build_message(
        to=[email],
        subject=subject,
        preheader=f"Your {app_name} verification code",
        eyebrow="Security",
        **content,
    )
    sent = await get_mail_sender().send(msg)
    if not sent:
        _log.error("Failed to send OTP email to %s", email)
    else:
        event = "otp_sent" if resend_count == 0 else "otp_resent"
        await log_auth_event(db, event_type=event, user_id=user_id, request=request)


async def signup_user(
    db: Database,
    *,
    name: str,
    email: str,
    password: str,
    confirm_password: str,
    request: Request | None = None,
) -> None:
    settings = get_settings()
    validate_password(password, confirm=confirm_password)
    email_norm = email.strip().lower()
    if not email_norm or "@" not in email_norm:
        raise BadRequestError("Valid email is required.")
    if len(name.strip()) < 2:
        raise BadRequestError("Name must be at least 2 characters.")

    existing = await db.fetch_one(
        """
        SELECT id, status FROM AIVA_users WHERE LOWER(email) = LOWER(:email)
        """,
        {"email": email_norm},
    )

    if existing:
        status = str(existing.get("status") or "")
        user_id = int(existing["id"])
        if status == STATUS_ACTIVE:
            await log_auth_event(
                db,
                event_type="signup_started",
                user_id=user_id,
                request=request,
                metadata={"existing_active": True},
            )
            return
        if status == STATUS_PENDING_EMAIL_VERIFICATION:
            await _maybe_resend_signup_otp(db, user_id=user_id, email=email_norm, request=request)
            return
        return

    organization_id = await _resolve_signup_organization_id(db, settings)
    role_id = await _resolve_signup_role_id(db, settings)
    first_name, last_name = _split_name(name)

    user_id = await db.execute(
        """
        INSERT INTO AIVA_users (
            organization_id, first_name, last_name, email, password_hash, status
        ) VALUES (
            :organization_id, :first_name, :last_name, :email, :password_hash, :status
        )
        RETURNING id INTO :out_id
        """,
        {
            "organization_id": organization_id,
            "first_name": first_name,
            "last_name": last_name,
            "email": email_norm,
            "password_hash": hash_password(password),
            "status": STATUS_PENDING_EMAIL_VERIFICATION,
        },
        return_id=True,
    )
    await db.execute(
        """
        INSERT INTO AIVA_user_roles (user_id, role_id, account_id)
        VALUES (:user_id, :role_id, NULL)
        """,
        {"user_id": user_id, "role_id": role_id},
    )
    await _invalidate_unused_otps(db, int(user_id or 0), OTP_PURPOSE_SIGNUP)
    await _create_and_send_otp(
        db,
        user_id=int(user_id or 0),
        email=email_norm,
        purpose=OTP_PURPOSE_SIGNUP,
        request=request,
    )
    await log_auth_event(
        db,
        event_type="signup_completed",
        user_id=int(user_id or 0),
        request=request,
    )


async def _maybe_resend_signup_otp(
    db: Database,
    *,
    user_id: int,
    email: str,
    request: Request | None,
) -> None:
    settings = get_settings()
    latest = await db.fetch_one(
        """
        SELECT id, resend_count, last_sent_at
        FROM AIVA_email_otps
        WHERE user_id = :user_id AND purpose = :purpose AND used_at IS NULL
        ORDER BY created_at DESC
        FETCH FIRST 1 ROW ONLY
        """,
        {"user_id": user_id, "purpose": OTP_PURPOSE_SIGNUP},
    )
    if latest and latest.get("last_sent_at"):
        last_sent = latest["last_sent_at"]
        if hasattr(last_sent, "timestamp"):
            elapsed = datetime.now(timezone.utc).timestamp() - last_sent.timestamp()
            if elapsed < settings.otp_resend_cooldown_seconds:
                return

    await _invalidate_unused_otps(db, user_id, OTP_PURPOSE_SIGNUP)
    resend_count = int(latest.get("resend_count") or 0) + 1 if latest else 0
    await _create_and_send_otp(
        db,
        user_id=user_id,
        email=email,
        purpose=OTP_PURPOSE_SIGNUP,
        request=request,
        resend_count=resend_count,
    )


async def resend_signup_otp(
    db: Database,
    *,
    email: str,
    request: Request | None = None,
) -> None:
    email_norm = email.strip().lower()
    user = await db.fetch_one(
        "SELECT id, status FROM AIVA_users WHERE LOWER(email) = LOWER(:email)",
        {"email": email_norm},
    )
    if not user or user.get("status") != STATUS_PENDING_EMAIL_VERIFICATION:
        return

    user_id = int(user["id"])
    settings = get_settings()

    lock_row = await db.fetch_one(
        "SELECT otp_locked_until FROM AIVA_users WHERE id = :id",
        {"id": user_id},
    )
    locked_until = lock_row.get("otp_locked_until") if lock_row else None
    if locked_until and hasattr(locked_until, "timestamp") and locked_until.timestamp() > datetime.now(timezone.utc).timestamp():
        return

    latest = await db.fetch_one(
        """
        SELECT resend_count, last_sent_at
        FROM AIVA_email_otps
        WHERE user_id = :user_id AND purpose = :purpose
        ORDER BY created_at DESC
        FETCH FIRST 1 ROW ONLY
        """,
        {"user_id": user_id, "purpose": OTP_PURPOSE_SIGNUP},
    )
    if latest and latest.get("last_sent_at"):
        last_sent = latest["last_sent_at"]
        if hasattr(last_sent, "timestamp"):
            elapsed = datetime.now(timezone.utc).timestamp() - last_sent.timestamp()
            if elapsed < settings.otp_resend_cooldown_seconds:
                return
        if int(latest.get("resend_count") or 0) >= settings.otp_max_resend_per_hour:
            return

    await _maybe_resend_signup_otp(db, user_id=user_id, email=email_norm, request=request)


async def verify_signup_otp(
    db: Database,
    *,
    email: str,
    otp: str,
    request: Request | None = None,
) -> None:
    email_norm = email.strip().lower()
    if not otp.isdigit() or len(otp) != get_settings().otp_length:
        raise UnauthorizedError("Invalid or expired verification code.")

    user = await db.fetch_one(
        "SELECT id, status, otp_locked_until FROM AIVA_users WHERE LOWER(email) = LOWER(:email)",
        {"email": email_norm},
    )
    if not user:
        raise UnauthorizedError("Invalid or expired verification code.")

    user_id = int(user["id"])
    locked_until = user.get("otp_locked_until")
    if locked_until and hasattr(locked_until, "timestamp") and locked_until.timestamp() > datetime.now(timezone.utc).timestamp():
        raise UnauthorizedError("Invalid or expired verification code.")

    row = await db.fetch_one(
        """
        SELECT id, otp_hash, expires_at, failed_attempts
        FROM AIVA_email_otps
        WHERE user_id = :user_id AND purpose = :purpose AND used_at IS NULL
        ORDER BY created_at DESC
        FETCH FIRST 1 ROW ONLY
        """,
        {"user_id": user_id, "purpose": OTP_PURPOSE_SIGNUP},
    )
    if not row:
        raise UnauthorizedError("Invalid or expired verification code.")

    expires_at = row.get("expires_at")
    if expires_at and hasattr(expires_at, "timestamp") and expires_at.timestamp() < datetime.now(timezone.utc).timestamp():
        raise UnauthorizedError("Invalid or expired verification code.")

    if row.get("otp_hash") != _hash_otp(otp):
        failed = int(row.get("failed_attempts") or 0) + 1
        await db.execute(
            "UPDATE AIVA_email_otps SET failed_attempts = :failed WHERE id = :id",
            {"failed": failed, "id": int(row["id"])},
        )
        await db.execute(
            "UPDATE AIVA_users SET otp_failed_attempts = otp_failed_attempts + 1 WHERE id = :id",
            {"id": user_id},
        )
        settings = get_settings()
        if failed >= settings.otp_max_attempts:
            await db.execute(
                """
                UPDATE AIVA_users
                SET otp_locked_until = SYSTIMESTAMP + NUMTODSINTERVAL(:mins, 'MINUTE')
                WHERE id = :id
                """,
                {"mins": settings.login_lock_minutes, "id": user_id},
            )
        await log_auth_event(db, event_type="otp_failed", user_id=user_id, request=request)
        raise UnauthorizedError("Invalid or expired verification code.")

    await db.execute(
        "UPDATE AIVA_email_otps SET used_at = SYSTIMESTAMP WHERE id = :id",
        {"id": int(row["id"])},
    )
    await db.execute(
        """
        UPDATE AIVA_users
        SET status = :status,
            email_verified_at = SYSTIMESTAMP,
            otp_failed_attempts = 0,
            otp_locked_until = NULL
        WHERE id = :id
        """,
        {"status": STATUS_ACTIVE, "id": user_id},
    )
    await log_auth_event(db, event_type="otp_verified", user_id=user_id, request=request)


async def request_password_reset(
    db: Database,
    *,
    email: str,
    request: Request | None = None,
) -> None:
    email_norm = email.strip().lower()
    user = await db.fetch_one(
        "SELECT id, status FROM AIVA_users WHERE LOWER(email) = LOWER(:email)",
        {"email": email_norm},
    )
    if not user or user.get("status") != STATUS_ACTIVE:
        return

    user_id = int(user["id"])
    await _invalidate_unused_otps(db, user_id, OTP_PURPOSE_FORGOT_PASSWORD)
    await _create_and_send_otp(
        db,
        user_id=user_id,
        email=email_norm,
        purpose=OTP_PURPOSE_FORGOT_PASSWORD,
        request=request,
    )
    await log_auth_event(db, event_type="forgot_password_requested", user_id=user_id, request=request)


async def reset_password_with_otp(
    db: Database,
    *,
    email: str,
    otp: str,
    password: str,
    confirm_password: str,
    request: Request | None = None,
) -> None:
    validate_password(password, confirm=confirm_password)
    email_norm = email.strip().lower()
    if not otp.isdigit() or len(otp) != get_settings().otp_length:
        raise BadRequestError("Invalid or expired reset code.")

    user = await db.fetch_one(
        "SELECT id, status FROM AIVA_users WHERE LOWER(email) = LOWER(:email)",
        {"email": email_norm},
    )
    if not user:
        raise BadRequestError("Invalid or expired reset code.")

    user_id = int(user["id"])
    row = await db.fetch_one(
        """
        SELECT id, otp_hash, expires_at
        FROM AIVA_email_otps
        WHERE user_id = :user_id AND purpose = :purpose AND used_at IS NULL
        ORDER BY created_at DESC
        FETCH FIRST 1 ROW ONLY
        """,
        {"user_id": user_id, "purpose": OTP_PURPOSE_FORGOT_PASSWORD},
    )
    if not row:
        raise BadRequestError("Invalid or expired reset code.")

    expires_at = row.get("expires_at")
    if expires_at and hasattr(expires_at, "timestamp") and expires_at.timestamp() < datetime.now(timezone.utc).timestamp():
        raise BadRequestError("Invalid or expired reset code.")

    if row.get("otp_hash") != _hash_otp(otp):
        raise BadRequestError("Invalid or expired reset code.")

    await db.execute(
        "UPDATE AIVA_email_otps SET used_at = SYSTIMESTAMP WHERE id = :id",
        {"id": int(row["id"])},
    )
    await db.execute(
        """
        UPDATE AIVA_users
        SET password_hash = :password_hash,
            failed_login_attempts = 0,
            locked_until = NULL
        WHERE id = :id
        """,
        {"password_hash": hash_password(password), "id": user_id},
    )
    await log_auth_event(db, event_type="password_reset_success", user_id=user_id, request=request)
