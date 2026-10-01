"""Command-line entry point for ``dr-ansible``.

Exit codes: ``0`` nothing needs attention, ``1`` findings were reported, ``2``
a usage error or a project that cannot be read. A module that fails to parse
is reported with the ``error`` status and does not stop the run (NFR-8).

The ``returns`` and ``draft`` subcommands are added in later plan tasks.
"""

from collections.abc import Sequence
from pathlib import Path

import click

from dr_ansible import __version__
from dr_ansible.config import Config, ConfigError, load_config
from dr_ansible.discovery import (
    DiscoveryError,
    Project,
    detect_project,
    discover_modules,
    filter_modules,
)
from dr_ansible.model import ReturnStatus
from dr_ansible.pipeline import analyze, has_findings
from dr_ansible.report import audit_markdown, audit_table, reports_to_json

EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2

FORMATS = ("table", "json", "markdown")


class FatalError(click.ClickException):
    """A problem with the project or the options: nothing can be reported."""

    exit_code = EXIT_ERROR


def _split(values: Sequence[str]) -> list[str]:
    """Values of a repeatable option that also accepts comma-separated lists."""
    return [
        part.strip() for value in values for part in value.split(",") if part.strip()
    ]


def _statuses(
    ctx: click.Context, param: click.Parameter, values: Sequence[str]
) -> frozenset[ReturnStatus]:
    statuses = set()
    for value in _split(values):
        try:
            statuses.add(ReturnStatus(value))
        except ValueError:
            known = ", ".join(s.value for s in ReturnStatus)
            raise click.BadParameter(
                f"{value!r} is not a status (choose from {known})"
            ) from None
    return frozenset(statuses)


def _load(path: Path, config_file: Path | None) -> tuple[Project, Config]:
    try:
        project = detect_project(path.resolve())
        return project, load_config(project.root, config_file)
    except (DiscoveryError, ConfigError) as exc:
        raise FatalError(str(exc)) from exc


@click.group()
@click.version_option(__version__, prog_name="dr-ansible")
def main() -> None:
    """Find Ansible modules with missing RETURN docs and draft them from evidence."""


_path_argument = click.argument(
    "path", type=click.Path(exists=True, file_okay=False, path_type=Path)
)
_config_option = click.option(
    "--config",
    "config_file",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Settings file to use instead of dr-ansible.toml or pyproject.toml.",
)
_format_option = click.option(
    "--format",
    "output_format",
    type=click.Choice(FORMATS),
    default="table",
    show_default=True,
    help="Output format.",
)


@main.command()
@_path_argument
@click.option(
    "-m",
    "--module",
    "modules",
    multiple=True,
    metavar="NAME",
    help="Only these modules: names, aliases or globs; repeat or comma-separate.",
)
@click.option(
    "--status",
    "statuses",
    multiple=True,
    callback=_statuses,
    metavar="STATUS",
    help="Only modules with these RETURN statuses, e.g. missing,placeholder.",
)
@_format_option
@_config_option
def audit(
    path: Path,
    modules: Sequence[str],
    statuses: frozenset[ReturnStatus],
    output_format: str,
    config_file: Path | None,
) -> None:
    """List every module in PATH with its RETURN status and key counts.

    PATH is an ansible-core checkout or a collection. Exits 1 if any listed
    module has a missing, placeholder or invalid RETURN, an analysis error,
    or keys that are undocumented or stale.
    """
    project, config = _load(path, config_file)
    try:
        selected = filter_modules(discover_modules(project), _split(modules))
    except DiscoveryError as exc:
        raise FatalError(str(exc)) from exc

    reports = [analyze(module, config).report for module in selected]
    if statuses:
        reports = [r for r in reports if r.return_status in statuses]

    if output_format == "json":
        text = reports_to_json(reports, project.root, config)
    elif output_format == "markdown":
        text = audit_markdown(reports)
    else:
        text = audit_table(reports)
    click.echo(text, nl=False)

    findings = any(has_findings(report) for report in reports)
    raise SystemExit(EXIT_FINDINGS if findings else EXIT_CLEAN)
