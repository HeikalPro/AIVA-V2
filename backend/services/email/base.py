from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EmailMessage:
    to: list[str]
    subject: str
    text_body: str
    html_body: str | None = None
