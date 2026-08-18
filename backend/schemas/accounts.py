import re

from pydantic import BaseModel, Field, field_validator

from backend.schemas.widget_features import WidgetFeaturesConfig


def _normalize_kb_source_base_url(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # Fix …?id=/ → …?id= (common misconfiguration)
    text = re.sub(r"=\s*/\s*$", "=", text)
    return text


class AccountCreate(BaseModel):
    organization_id: int
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    corpus_id: str | None = None
    llm_config_id: int | None = None
    status: str = "ACTIVE"
    kb_source_base_url: str | None = None
    widget_features: WidgetFeaturesConfig | None = None

    @field_validator("kb_source_base_url", mode="before")
    @classmethod
    def normalize_kb_url_create(cls, v: object | None) -> str | None:
        return _normalize_kb_source_base_url(v)


class AccountUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    corpus_id: str | None = None
    llm_config_id: int | None = None
    status: str | None = None
    kb_source_base_url: str | None = None
    widget_features: WidgetFeaturesConfig | None = None

    @field_validator("kb_source_base_url", mode="before")
    @classmethod
    def normalize_kb_url_update(cls, v: object | None) -> str | None:
        return _normalize_kb_source_base_url(v)


class AccountOut(BaseModel):
    id: int
    organization_id: int
    organization_name: str | None = None
    organization_code: str | None = None
    llm_config_id: int | None
    name: str
    description: str | None
    corpus_id: str | None
    status: str
    kb_source_base_url: str | None = None
    widget_features: dict | None = None
    created_at: str | None = None
