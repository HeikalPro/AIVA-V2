"""`crm-ingest` command line: run the chain on a local file or a OneDrive/SharePoint item.

Exit codes: 0 success, 1 `config check` found a broken setting, 2 any error
(an `IngestionError`, an unreadable file, invalid settings or bad arguments)."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from . import __version__
from .app import CRMIngestionApp
from .config import ExtractionSettings, Settings
from .connectors.sharepoint.models import DriveItemReference
from .crm.service import CRMProcessingResult, registry_from_settings
from .errors import IngestionError
from .ingestion.pipeline import DOCUMENT_EXTRACTOR_CONFIG_ENV

__all__ = ["build_parser", "main"]

PROG = "crm-ingest"
EXIT_OK = 0
EXIT_CONFIG_PROBLEM = 1
EXIT_ERROR = 2

AppFactory = Callable[[Settings], CRMIngestionApp]


def _default_app_factory(settings: Settings) -> CRMIngestionApp:
    return CRMIngestionApp(settings=settings)


# ---- parser -------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """The argparse tree for `crm-ingest`."""
    parser = argparse.ArgumentParser(
        prog=PROG, description="Extract CRM entities from documents (local files or OneDrive/SharePoint)."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-o", "--output", type=Path, help="write the JSON here instead of stdout")
    common.add_argument(
        "--mode",
        choices=("fast", "balanced", "accurate"),
        help="document-extractor mode (overrides CRM_EXTRACTION_MODE; ignored when "
        "DOCUMENT_EXTRACTOR_CONFIG is set)",
    )
    common.add_argument(
        "--crm-only", action="store_true", help="output only the flat CRM entity list, not the full result"
    )

    p_file = sub.add_parser("file", parents=[common], help="process a local file")
    p_file.add_argument("path", type=Path, help="the document to process")

    p_sp = sub.add_parser("sharepoint", parents=[common], help="process a OneDrive/SharePoint file")
    where = p_sp.add_mutually_exclusive_group(required=True)
    where.add_argument("--url", help="a sharing link or file URL")
    where.add_argument("--drive-id", help="the drive id (use with --item-id)")
    p_sp.add_argument("--item-id", help="the drive item id (use with --drive-id)")

    p_cfg = sub.add_parser("config", help="inspect settings")
    cfg_sub = p_cfg.add_subparsers(dest="config_command", required=True, metavar="ACTION")
    cfg_sub.add_parser("check", help="show which settings are set (never prints secrets)")

    p_sch = sub.add_parser("schemas", help="inspect entity schemas")
    sch_sub = p_sch.add_subparsers(dest="schemas_command", required=True, metavar="ACTION")
    p_list = sch_sub.add_parser("list", help="list registered entity schemas")
    p_list.add_argument("--json", action="store_true", help="print the full schema definitions as JSON")
    return parser


# ---- output helpers -----------------------------------------------------------------


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _error(message: str) -> None:
    print(f"{PROG}: error: {_one_line(message)}", file=sys.stderr)


def _dumps(data: Any, *, ascii_only: bool) -> str:
    return json.dumps(data, ensure_ascii=ascii_only, indent=2) + "\n"


def _write_json(data: Any, output: Path | None) -> None:
    """UTF-8 to a file; to stdout, escape non-ASCII unless the console is UTF-8."""
    if output is not None:
        output.write_text(_dumps(data, ascii_only=False), encoding="utf-8")
        return
    encoding = (getattr(sys.stdout, "encoding", None) or "").lower().replace("-", "").replace("_", "")
    sys.stdout.write(_dumps(data, ascii_only=encoding != "utf8"))


def _settings_for(args: argparse.Namespace) -> Settings:
    settings = Settings()
    if getattr(args, "mode", None):
        settings = settings.model_copy(update={"extraction": ExtractionSettings(mode=args.mode)})
    return settings


# ---- commands -----------------------------------------------------------------------


def _emit_result(result: CRMProcessingResult, args: argparse.Namespace) -> int:
    data: Any = result.to_crm_json() if args.crm_only else result.to_dict()
    _write_json(data, args.output)
    if args.output is not None:
        status = "valid" if result.valid else "has validation errors"
        print(
            f"{PROG}: wrote {len(result.extraction.entities)} entities to {args.output} ({status})",
            file=sys.stderr,
        )
    return EXIT_OK


def _cmd_file(args: argparse.Namespace, app_factory: AppFactory) -> int:
    path: Path = args.path
    if not path.is_file():
        _error(f"file not found: {path}")
        return EXIT_ERROR
    app = app_factory(_settings_for(args))
    with app:
        result = app.process_file(path)
    return _emit_result(result, args)


def _cmd_sharepoint(
    args: argparse.Namespace, app_factory: AppFactory, parser: argparse.ArgumentParser
) -> int:
    if args.url is not None:
        if args.item_id is not None:
            parser.error("--item-id is only valid with --drive-id")
        reference = DriveItemReference.from_sharing_url(args.url)
    else:
        if not args.item_id:
            parser.error("--drive-id requires --item-id")
        reference = DriveItemReference.from_ids(args.drive_id, args.item_id)
    app = app_factory(_settings_for(args))
    with app:
        result = app.process_reference(reference)
    return _emit_result(result, args)


def _state(value: object) -> str:
    return "set" if value else "missing"


def _extractor_config_status(path_value: str) -> list[str]:
    path = Path(path_value)
    lines = [f"  {DOCUMENT_EXTRACTOR_CONFIG_ENV:<32} {path}"]
    if not path.is_file():
        lines.append("    WARNING: file does not exist; document-extractor will fail to load it")
        return lines
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        lines.append(f"    WARNING: not readable as TOML: {_one_line(str(exc))}")
        return lines
    intelligence = data.get("intelligence")
    if isinstance(intelligence, dict) and intelligence.get("enabled") is True:
        endpoint = intelligence.get("base_url") or "(library default endpoint)"
        lines.append(f"    WARNING: [intelligence] enabled = true: document text is sent to {endpoint}")
    lines.append("    CRM_EXTRACTION_MODE is ignored while this is set")
    return lines


def _cmd_config_check(settings: Settings) -> int:
    ms = settings.microsoft
    lines = ["Microsoft Graph (OneDrive / SharePoint)"]
    lines.append(f"  {'MICROSOFT_TENANT_ID':<32} {_state(ms.tenant_id)}")
    lines.append(f"  {'MICROSOFT_CLIENT_ID':<32} {_state(ms.client_id)}")
    lines.append(f"  {'MICROSOFT_CLIENT_SECRET':<32} {_state(ms.client_secret.get_secret_value())}")
    lines.append(f"  {'MICROSOFT_GRAPH_BASE_URL':<32} {ms.graph_base_url}")
    lines.append(f"  {'MICROSOFT_AUTHORITY_HOST':<32} {ms.authority_host}")
    lines.append(f"  {'MICROSOFT_GRAPH_TIMEOUT_SECONDS':<32} {ms.graph_timeout_seconds}")
    lines.append(f"  {'MICROSOFT_MAX_DOWNLOAD_BYTES':<32} {ms.max_download_bytes}")
    msal_ok = importlib.util.find_spec("msal") is not None
    lines.append(f"  {'msal package':<32} {'installed' if msal_ok else 'not installed'}")
    if not ms.is_configured:
        lines.append(f"  -> SharePoint is not configured (missing: {', '.join(ms.missing())})")
        lines.append("     Local files still work: `crm-ingest file PATH`.")
    elif not msal_ok:
        lines.append("  -> SharePoint is not usable: install the msal extra (`pip install '.[msal]'`)")
    else:
        lines.append("  -> SharePoint is configured (credentials are only verified on first use)")

    lines.append("document-extractor")
    de_config = os.environ.get(DOCUMENT_EXTRACTOR_CONFIG_ENV, "").strip()
    if de_config:
        lines.extend(_extractor_config_status(de_config))
    else:
        lines.append(f"  {DOCUMENT_EXTRACTOR_CONFIG_ENV:<32} not set")
        lines.append(f"  {'CRM_EXTRACTION_MODE':<32} {settings.extraction.mode}")

    exit_code = EXIT_OK
    lines.append("CRM")
    lines.append(f"  {'CRM_MIN_CONFIDENCE':<32} {settings.crm.min_confidence}")
    if not settings.crm.schema_paths:
        lines.append(f"  {'CRM_SCHEMA_PATHS':<32} none (generic schemas only)")
    else:
        lines.append(f"  {'CRM_SCHEMA_PATHS':<32} {len(settings.crm.schema_paths)} file(s)")
        for p in settings.crm.schema_paths:
            lines.append(f"    {p} ({'found' if Path(p).is_file() else 'NOT FOUND'})")
    try:
        registry = registry_from_settings(settings.crm)
    except (OSError, ValueError) as exc:  # pydantic.ValidationError is a ValueError
        lines.append(f"  -> schemas could not be loaded: {_one_line(str(exc))}")
        exit_code = EXIT_CONFIG_PROBLEM
    else:
        lines.append(f"  -> {len(registry)} entity schemas: {', '.join(registry.names())}")

    print("\n".join(lines))
    return exit_code


def _cmd_schemas_list(settings: Settings, as_json: bool) -> int:
    registry = registry_from_settings(settings.crm)
    if as_json:
        _write_json(registry.to_dict(), None)
        return EXIT_OK
    width = max((len(n) for n in registry.names()), default=0)
    for schema in registry:
        required = ", ".join(schema.required_fields) or "-"
        print(f"{schema.name:<{width}}  {len(schema.fields):>2} fields  required: {required}")
        if schema.description:
            print(f"{'':<{width}}   {schema.description}")
    return EXIT_OK


# ---- entry point --------------------------------------------------------------------


def main(argv: Sequence[str] | None = None, *, app_factory: AppFactory | None = None) -> int:
    """Run `crm-ingest`; returns the process exit code. `app_factory` lets tests inject
    a `CRMIngestionApp` built from the resolved settings."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help, --version, usage errors
        return exc.code if isinstance(exc.code, int) else EXIT_ERROR
    factory = app_factory if app_factory is not None else _default_app_factory
    try:
        if args.command == "file":
            return _cmd_file(args, factory)
        if args.command == "sharepoint":
            return _cmd_sharepoint(args, factory, parser)
        if args.command == "config":
            return _cmd_config_check(Settings())
        if args.command == "schemas":
            return _cmd_schemas_list(Settings(), args.json)
    except SystemExit as exc:  # parser.error() inside a command
        return exc.code if isinstance(exc.code, int) else EXIT_ERROR
    except IngestionError as exc:
        _error(f"{type(exc).__name__}: {exc}")
        return EXIT_ERROR
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'value'}: {e['msg']}" for e in exc.errors()
        )
        _error(f"invalid settings or input: {details}")
        return EXIT_ERROR
    except (OSError, ValueError) as exc:
        _error(f"{type(exc).__name__}: {exc}")
        return EXIT_ERROR
    except KeyboardInterrupt:
        return 130
    parser.error(f"unknown command {args.command!r}")  # pragma: no cover - argparse enforces choices


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
