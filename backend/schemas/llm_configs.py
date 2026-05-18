from pydantic import BaseModel, Field


class LLMConfigCreate(BaseModel):
    provider: str = Field(min_length=1, max_length=100)
    model_name: str = Field(min_length=1, max_length=255)
    api_base_url: str | None = None
    temperature: float | None = Field(default=0.7, ge=0, le=2)
    max_tokens: int | None = None
    embedding_model: str | None = None
    reranker_model: str | None = None
    is_active: bool = True


class LLMConfigUpdate(BaseModel):
    provider: str | None = Field(default=None, min_length=1, max_length=100)
    model_name: str | None = Field(default=None, min_length=1, max_length=255)
    api_base_url: str | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = None
    embedding_model: str | None = None
    reranker_model: str | None = None
    is_active: bool | None = None


class LLMConfigOut(BaseModel):
    id: int
    provider: str
    model_name: str
    api_base_url: str | None
    temperature: float | None
    max_tokens: int | None
    embedding_model: str | None
    reranker_model: str | None
    is_active: bool
    created_at: str | None
