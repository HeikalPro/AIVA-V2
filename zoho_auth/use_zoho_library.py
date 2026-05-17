"""Simple consumer file that recalls the zoho_auth library.

Run:
    python use_zoho_library.py
    python use_zoho_library.py --help
"""

from __future__ import annotations

import sys

from zoho_auth import login


if __name__ == "__main__":
    sys.exit(login(sys.argv[1:]))
