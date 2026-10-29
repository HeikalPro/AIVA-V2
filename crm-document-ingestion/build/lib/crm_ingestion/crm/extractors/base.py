"""The extractor contract: a Document in, CRM entities out."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from document_extractor import Document

    from ..schemas.base import CRMEntity
    from ..schemas.definitions import SchemaRegistry


class EntityExtractor(ABC):
    """Turns one document into CRM entities for the schemas in a registry.

    Implementations must not modify the document. Raising is allowed (a
    CompositeExtractor turns it into a warning); returning [] means nothing found."""

    name: str = "extractor"

    @abstractmethod
    def extract(self, document: Document, *, schemas: SchemaRegistry) -> list[CRMEntity]:
        """Entities found in `document`, most salient first within each entity type."""

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name!r})"
