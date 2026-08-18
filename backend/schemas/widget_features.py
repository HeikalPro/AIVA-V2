from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

VALID_INSTALLMENT_CALCULATOR_TYPES = frozenset({"cash-it", "instant-approval", "branches"})
VALID_CALCULATOR_TENORS = frozenset({6, 9, 12, 18, 24, 30, 36})

_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


class WidgetBrandingConfig(BaseModel):
    """Per-account widget branding (header text, accent color, logo)."""

    title: str | None = None
    subtitle: str | None = None
    accent_color: str | None = None
    logo_url: str | None = None

    @field_validator("title", "subtitle", mode="before")
    @classmethod
    def normalize_text(cls, value: object | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text[:60] if text else None

    @field_validator("accent_color", mode="before")
    @classmethod
    def normalize_accent(cls, value: object | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        if not _HEX_COLOR_RE.match(text):
            raise ValueError("accent_color must be a hex color like #0057A8")
        return text.lower()

    @field_validator("logo_url", mode="before")
    @classmethod
    def normalize_logo(cls, value: object | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        if not (text.startswith("https://") or text.startswith("http://") or text.startswith("data:image/")):
            raise ValueError("logo_url must be an http(s) or data:image URL")
        return text[:2000]


class CalculatorProductConfig(BaseModel):
    """Per-product calculator settings (display name, rates and tenors)."""

    label: str | None = None
    apr: float | None = None
    tenors: list[int] | None = None
    flat_rates: dict[str, float] | None = None

    @field_validator("label", mode="before")
    @classmethod
    def normalize_label(cls, value: object | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        return text[:60]

    @field_validator("apr", mode="before")
    @classmethod
    def normalize_apr(cls, value: object | None) -> float | None:
        if value is None or value == "":
            return None
        try:
            apr = float(value)
        except (TypeError, ValueError):
            return None
        if apr <= 0 or apr > 2:
            raise ValueError("apr must be between 0 and 2 (e.g. 0.55 for 55%)")
        return apr

    @field_validator("tenors", mode="before")
    @classmethod
    def normalize_tenors(cls, value: object | None) -> list[int] | None:
        if value is None:
            return None
        if not isinstance(value, list):
            return None
        out: list[int] = []
        seen: set[int] = set()
        for raw in value:
            try:
                month = int(raw)
            except (TypeError, ValueError):
                continue
            if month not in VALID_CALCULATOR_TENORS or month in seen:
                continue
            seen.add(month)
            out.append(month)
        return out or None

    @field_validator("flat_rates", mode="before")
    @classmethod
    def normalize_flat_rates(cls, value: object | None) -> dict[str, float] | None:
        if value is None or not isinstance(value, dict):
            return None
        out: dict[str, float] = {}
        for raw_month, raw_rate in value.items():
            month = str(raw_month).strip()
            if month not in {str(t) for t in VALID_CALCULATOR_TENORS}:
                continue
            try:
                rate = float(raw_rate)
            except (TypeError, ValueError):
                continue
            if rate <= 0 or rate > 1:
                continue
            out[month] = rate
        return out or None


class WidgetInstallmentCalculatorConfig(BaseModel):
    enabled: bool = False
    types: list[str] = Field(default_factory=lambda: list(VALID_INSTALLMENT_CALCULATOR_TYPES))
    products: dict[str, CalculatorProductConfig] | None = None

    @field_validator("types", mode="before")
    @classmethod
    def normalize_types(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            return list(VALID_INSTALLMENT_CALCULATOR_TYPES)
        out: list[str] = []
        seen: set[str] = set()
        for raw in value:
            key = str(raw).strip()
            if not key or key in seen or key not in VALID_INSTALLMENT_CALCULATOR_TYPES:
                continue
            seen.add(key)
            out.append(key)
        return out or list(VALID_INSTALLMENT_CALCULATOR_TYPES)

    @field_validator("products", mode="before")
    @classmethod
    def normalize_products(cls, value: object | None) -> dict[str, CalculatorProductConfig] | None:
        if value is None or not isinstance(value, dict):
            return None
        out: dict[str, CalculatorProductConfig] = {}
        for raw_key, raw_product in value.items():
            key = str(raw_key).strip()
            if key not in VALID_INSTALLMENT_CALCULATOR_TYPES:
                continue
            if isinstance(raw_product, CalculatorProductConfig):
                out[key] = raw_product
            elif isinstance(raw_product, dict):
                out[key] = CalculatorProductConfig.model_validate(raw_product)
        return out or None


class WidgetKbQueuesConfig(BaseModel):
    """Per-account visibility override for the KB queue buttons (Gomla/Halan/...).

    ``visible_keys is None`` means "no override" — the widget shows every queue
    the corpus/agent allows (current default). A list restricts the widget to
    only those queue keys.
    """

    visible_keys: list[str] | None = None

    @field_validator("visible_keys", mode="before")
    @classmethod
    def normalize_visible_keys(cls, value: object | None) -> list[str] | None:
        if value is None:
            return None
        if not isinstance(value, list):
            return None
        out: list[str] = []
        seen: set[str] = set()
        for raw in value:
            key = str(raw).strip()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(key)
        return out


class WidgetLocationItem(BaseModel):
    """One branch/office shown in the widget's locations panel."""

    name: str
    area: str | None = None
    address: str | None = None
    phone: str | None = None
    hours: str | None = None
    maps_url: str | None = None

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("location name is required")
        return text[:80]

    @field_validator("area", "phone", "hours", mode="before")
    @classmethod
    def normalize_short_text(cls, value: object | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text[:120] if text else None

    @field_validator("address", mode="before")
    @classmethod
    def normalize_address(cls, value: object | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text[:300] if text else None

    @field_validator("maps_url", mode="before")
    @classmethod
    def normalize_maps_url(cls, value: object | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        if not (text.startswith("https://") or text.startswith("http://")):
            raise ValueError("maps_url must be an http(s) URL")
        return text[:2000]


class WidgetLocationsConfig(BaseModel):
    """Per-account branch/location list surfaced as a widget panel."""

    enabled: bool = False
    items: list[WidgetLocationItem] = Field(default_factory=list)

    @field_validator("items", mode="before")
    @classmethod
    def normalize_items(cls, value: object | None) -> list[object]:
        if not isinstance(value, list):
            return []
        return value[:100]


class WidgetFeaturesConfig(BaseModel):
    installment_calculator: WidgetInstallmentCalculatorConfig | None = None
    kb_queues: WidgetKbQueuesConfig | None = None
    branding: WidgetBrandingConfig | None = None
    locations: WidgetLocationsConfig | None = None

    def calculator_enabled(self) -> bool:
        calc = self.installment_calculator
        return bool(calc and calc.enabled)

    def calculator_types(self) -> list[str]:
        calc = self.installment_calculator
        if not calc or not calc.enabled:
            return []
        return list(calc.types)

    def kb_visible_keys(self) -> list[str] | None:
        """Return the visibility allow-list, or None when no override is set."""
        kb = self.kb_queues
        if kb is None:
            return None
        return kb.visible_keys
