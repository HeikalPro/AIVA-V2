from pydantic import BaseModel, Field


class LLMConfigCreate(BaseModel):
    provider: str = Field(min_length=1, max_length=100)
    model_name: str = Field(min_length=1, max_length=255)
    comment: str | None = Field(default=None, max_length=512)
    api_base_url: str | None = None
    temperature: float | None = Field(default=0.7, ge=0, le=2)
    max_tokens: int | None = None
    embedding_model: str | None = None
    reranker_model: str | None = None
    is_active: bool = True


class LLMConfigUpdate(BaseModel):
    provider: str | None = Field(default=None, min_length=1, max_length=100)
    model_name: str | None = Field(default=None, min_length=1, max_length=255)
    comment: str | None = Field(default=None, max_length=512)
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
    comment: str | None
    api_base_url: str | None
    temperature: float | None
    max_tokens: int | None
    embedding_model: str | None
    reranker_model: str | None
    is_active: bool
    created_at: str | None


class ModelCatalogItem(BaseModel):
    id: str
    display_name: str | None = None
    provider: str | None = None
    modality: str | None = None
    context_window: int | None = None
    status: str | None = None
    input_per_1m_egp: float | None = None
    output_per_1m_egp: float | None = None
    currency: str = "EGP"


class ModelCatalogOut(BaseModel):
    items: list[ModelCatalogItem]
    error: str | None = None  # message from the most recent failed fetch, if any
    error_at: str | None = None  # ISO time of that failure
    last_success_at: str | None = None  # ISO time the catalog was last fetched OK
    stale: bool = False  # True when serving an older cached copy after a failed refresh
