"""Allow ``python -m zoho_auth`` from the repository root."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
