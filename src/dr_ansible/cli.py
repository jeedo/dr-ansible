"""Command-line entry point for ``dr-ansible``.

Exit codes: ``0`` nothing needs attention, ``1`` findings were reported, ``2``
a usage error or a project that cannot be read. A module that fails to parse
is reported with the ``error`` status and does not stop the run (NFR-8).

Nothing here writes to the analysed tree: reports and drafts go to stdout,
or to the file named by ``draft --output``, which must lie outside the
project's module and action plugin directories (NFR-5).
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
from dr_ansible.draft import render_draft, render_merge
from dr_ansible.model import ModuleInfo, ReturnStatus
from dr_ansible.pipeline import analyze, has_findings
from dr_ansible.report import (
    audit_markdown,
    audit_table,
    keys_markdown,
    keys_table,
    reports_to_json,
)

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


def _select_one(project: Project, name: str) -> ModuleInfo:
    """The one module ``name`` (a name, alias or glob) refers to."""
    try:
        matches = filter_modules(discover_modules(project), [name])
    except DiscoveryError as exc:
        raise FatalError(str(exc)) from exc
    if not matches:
        raise FatalError(f"no module matches {name!r}")
    if len(matches) > 1:
        names = ", ".join(sorted(m.fqcn for m in matches))
        raise FatalError(f"{name!r} matches {len(matches)} modules ({names})")
    return matches[0]


def _exit(findings: bool) -> None:
    raise SystemExit(EXIT_FINDINGS if findings else EXIT_CLEAN)


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

    _exit(any(has_findings(report) for report in reports))


@main.command()
@_path_argument
@click.argument("module")
@click.option(
    "--include-common",
    is_flag=True,
    help="Also show common return values such as changed, failed and msg.",
)
@_format_option
@_config_option
def returns(
    path: Path,
    module: str,
    include_common: bool,
    output_format: str,
    config_file: Path | None,
) -> None:
    """Show each return key of MODULE in PATH, with its evidence and status.

    MODULE is a name, alias or FQCN, or a glob matching exactly one module.
    Exits 1 if the module has findings, as for audit.
    """
    project, config = _load(path, config_file)
    selected = _select_one(project, module)
    report = analyze(selected, config, include_common=include_common).report

    if output_format == "json":
        text = reports_to_json([report], project.root, config)
    elif output_format == "markdown":
        text = keys_markdown(report, project.root)
    else:
        text = keys_table(report, project.root)
    click.echo(text, nl=False)
    _exit(has_findings(report))


@main.command()
@_path_argument
@click.argument("module")
@click.option(
    "--merge/--full",
    default=True,
    show_default=True,
    help="Add only the missing keys to the existing RETURN, or draft it all anew.",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Write the draft to this file instead of stdout.",
)
@_config_option
def draft(
    path: Path,
    module: str,
    merge: bool,
    output: Path | None,
    config_file: Path | None,
) -> None:
    """Print a draft RETURN block for MODULE in PATH.

    Every description is the DR-ANSIBLE-TODO marker for a human to replace.
    In merge mode (the default) existing entries are kept byte for byte.
    Module files are never modified.
    """
    project, config = _load(path, config_file)
    if output is not None:
        _check_output(project, output)
    selected = _select_one(project, module)
    result = analyze(selected, config)
    report = result.report
    if report.return_status is ReturnStatus.UNSUPPORTED:
        raise FatalError(f"{report.fqcn}: PowerShell modules are not supported")
    if report.error is not None or result.docs is None:
        raise FatalError(f"cannot draft {report.fqcn}: {report.error}")

    if merge:
        text = render_merge(report, result.docs, config, project.root)
    else:
        text = render_draft(report, config, project.root)

    if output is None:
        click.echo(text, nl=False)
        return
    try:
        output.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise FatalError(f"cannot write {output}: {exc.strerror or exc}") from exc
    click.echo(f"wrote draft for {report.fqcn} to {output}", err=True)


def _check_output(project: Project, output: Path) -> None:
    """Refuse an ``--output`` among the module or action plugin files (NFR-5)."""
    target = output.resolve()
    for directory in (project.modules_dir, project.actions_dir):
        if target.is_relative_to(directory.resolve()):
            raise FatalError(
                f"refusing to write {output}: it is inside {directory}, and"
                " dr-ansible never modifies module or plugin files"
            )
