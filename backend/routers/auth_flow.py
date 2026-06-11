from fastapi import APIRouter, Request

from backend.config import get_settings
from backend.dependencies import DbDep
from backend.limiter import limiter
from backend.schemas.auth import (
    ForgotPasswordRequest,
    ResendOtpRequest,
    ResetPasswordRequest,
    SignupRequest,
    VerifyEmailOtpRequest,
)
from backend.schemas.common import MessageResponse
from backend.services.otp_service import (
    request_password_reset,
    resend_signup_otp,
    reset_password_with_otp,
    signup_user,
    verify_signup_otp,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/signup", response_model=MessageResponse)
@limiter.limit(get_settings().rate_limit_signup)
async def signup(body: SignupRequest, request: Request, db: DbDep) -> MessageResponse:
    await signup_user(
        db,
        name=body.name,
        email=body.email,
        password=body.password,
        confirm_password=body.confirm_password,
        request=request,
    )
    return MessageResponse(
        message="Your account has been created. Please check your email for the verification code."
    )


@router.post("/verify-email-otp", response_model=MessageResponse)
@limiter.limit(get_settings().rate_limit_login)
async def verify_email_otp(
    body: VerifyEmailOtpRequest, request: Request, db: DbDep
) -> MessageResponse:
    await verify_signup_otp(db, email=body.email, otp=body.otp, request=request)
    return MessageResponse(message="Email verified successfully.")


@router.post("/resend-email-otp", response_model=MessageResponse)
@limiter.limit(get_settings().rate_limit_resend_otp)
async def resend_email_otp(
    body: ResendOtpRequest, request: Request, db: DbDep
) -> MessageResponse:
    await resend_signup_otp(db, email=body.email, request=request)
    return MessageResponse(
        message="A new verification code has been sent if your request is valid."
    )


@router.post("/forgot-password", response_model=MessageResponse)
@limiter.limit(get_settings().rate_limit_forgot_password)
async def forgot_password(
    body: ForgotPasswordRequest, request: Request, db: DbDep
) -> MessageResponse:
    await request_password_reset(db, email=body.email, request=request)
    return MessageResponse(
        message="If an account exists for this email, you will receive password reset instructions."
    )


@router.post("/reset-password", response_model=MessageResponse)
@limiter.limit(get_settings().rate_limit_login)
async def reset_password(
    body: ResetPasswordRequest, request: Request, db: DbDep
) -> MessageResponse:
    await reset_password_with_otp(
        db,
        email=body.email,
        otp=body.otp,
        password=body.password,
        confirm_password=body.confirm_password,
        request=request,
    )
    return MessageResponse(
        message="Your password has been updated successfully. Please login again."
    )
