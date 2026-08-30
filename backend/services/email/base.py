from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class InlineImage:
    """An image embedded in the message body and referenced as ``cid:<cid>``.

    ``fallback_url`` is used by senders that cannot carry attachments (the Zoho
    Mail API), which rewrite the ``cid:`` reference to this public URL instead.
    """

    cid: str
    path: Path
    fallback_url: str


@dataclass(frozen=True)
class EmailMessage:
    to: list[str]
    subject: str
    text_body: str
    html_body: str | None = None
    inline_images: tuple[InlineImage, ...] = field(default_factory=tuple)
