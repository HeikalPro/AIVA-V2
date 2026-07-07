from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

VALID_INSTALLMENT_CALCULATOR_TYPES = frozenset({"cash-it", "instant-approval", "branches"})
VALID_CALCULATOR_TENORS = frozenset({6, 9, 12, 18, 24, 30, 36})


class CalculatorProductConfig(BaseModel):
    """Per-product calculator settings (rates and tenors)."""

    apr: float | None = None
    tenors: list[int] | None = None
    flat_rates: dict[str, float] | None = None

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


class WidgetFeaturesConfig(BaseModel):
    installment_calculator: WidgetInstallmentCalculatorConfig | None = None

    def calculator_enabled(self) -> bool:
        calc = self.installment_calculator
        return bool(calc and calc.enabled)

    def calculator_types(self) -> list[str]:
        calc = self.installment_calculator
        if not calc or not calc.enabled:
            return []
        return list(calc.types)
