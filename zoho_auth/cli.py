"""Argparse driver for the ``ZohoLoginApp``."""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from .app import ZohoLoginApp
from .container import ServiceContainer
from .exceptions import ZohoConfigError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Zoho OAuth login in the terminal (browser handles credentials)."
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open a browser; print the authorization URL only.",
    )
    parser.add_argument(
        "--no-prompt",
        action="store_true",
        help="Skip the 'Press Enter to continue' step (useful for scripts).",
    )
    parser.add_argument(
        "--force-login",
        action="store_true",
        help="Ignore any saved session and run the full browser sign-in.",
    )
    parser.add_argument(
        "--logout",
        action="store_true",
        help="Revoke the saved refresh token and clear local session storage.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        container = ServiceContainer()
    except ZohoConfigError as exc:
        print(f"[ERROR] Configuration: {exc}", file=sys.stderr, flush=True)
        return ZohoLoginApp.EXIT_FAILURE

    if args.logout:
        container.auth_service.logout()
        return ZohoLoginApp.EXIT_OK

    app = ZohoLoginApp(
        container,
        open_browser=not args.no_browser,
        prompt_before_browser=not args.no_prompt,
        force_browser=args.force_login,
    )
    return app.run()
