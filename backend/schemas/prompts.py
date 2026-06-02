from pydantic import BaseModel, Field


class PromptCreate(BaseModel):
    account_id: int
    prompt_name: str = Field(min_length=1, max_length=255)
    prompt_type: str | None = None
    prompt_text: str = Field(min_length=1)
    is_active: bool = True


class PromptUpdate(BaseModel):
    prompt_name: str | None = Field(default=None, min_length=1, max_length=255)
    prompt_type: str | None = None
    prompt_text: str | None = Field(default=None, min_length=1)
    is_active: bool | None = None


class PromptOut(BaseModel):
    id: int
    account_id: int
    prompt_name: str
    prompt_type: str | None
    prompt_text: str
    version_number: int
    is_active: bool
    created_by: int | None
    created_at: str | None


class SystemPromptOut(BaseModel):
    prompt_text: str
    updated_at: str | None = None


class SystemPromptUpdate(BaseModel):
    prompt_text: str = Field(min_length=1)
