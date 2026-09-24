"""Graph sharing-URL encoding (https://learn.microsoft.com/graph/api/shares-get)."""

from __future__ import annotations

import base64


def encode_sharing_url(url: str) -> str:
    """Encode a sharing URL as a Graph share id: "u!" + unpadded base64url of the URL."""
    if not url or not url.strip():
        raise ValueError("sharing url must not be empty")
    encoded = base64.urlsafe_b64encode(url.strip().encode("utf-8")).decode("ascii")
    return "u!" + encoded.rstrip("=")
