"""Typer CLI — install with `pip install 'llm-service[cli]'`."""

from __future__ import annotations

import typer
from rich import print as rprint

app = typer.Typer(no_args_is_help=True, help="llm-service command-line utilities")


@app.command("version")
def version_cmd() -> None:
    rprint("[bold]llm-service[/bold] — see pyproject.toml / pip show llm-service")


@app.command("providers")
def providers_cmd() -> None:
    from llm_service.providers.registry import list_providers

    for p in list_providers():
        rprint(p)


@app.command("config-validate")
def config_validate(path: str) -> None:
    from llm_service.config.settings import LibrarySettings

    LibrarySettings.from_file(path)
    rprint("[green]Configuration OK[/green]")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
