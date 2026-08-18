from pydantic import BaseModel, EmailStr, Field, field_validator

from backend.utils import normalize_allowed_email


class TraineeCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    first_name: str | None = None
    last_name: str | None = None
    account_id: int
    status: str = "ACTIVE"

    @field_validator("email", mode="before")
    @classmethod
    def enforce_email_domain(cls, v: object) -> str:
        if not isinstance(v, str):
            raise ValueError("Email is required")
        return normalize_allowed_email(v)
