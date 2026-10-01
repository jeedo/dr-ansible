"""Markdown tables, for pull requests and issues (FR-19)."""

from collections.abc import Iterable, Sequence
from pathlib import Path

from dr_ansible.model import ModuleReport
from dr_ansible.report.summary import by_fqcn, counts, key_cells, relative


def audit_markdown(reports: Iterable[ModuleReport]) -> str:
    """The audit summary as a markdown table, one row per module."""
    lines = [
        _row(("Module", "RETURN", "Doc", "Code", "Tests", "Undoc", "Stale")),
        "|---|---|--:|--:|--:|--:|--:|",
    ]
    for report in by_fqcn(reports):
        c = counts(report)
        numbers = (c.documented, c.code, c.tests, c.undocumented, c.stale)
        lines.append(
            _row(
                (
                    _code(report.fqcn),
                    _escape(str(report.return_status)),
                    *map(str, numbers),
                )
            )
        )
    return "".join(f"{line}\n" for line in lines)


def keys_markdown(report: ModuleReport, root: Path) -> str:
    """One module's keys as a markdown section."""
    lines = [f"## {_code(report.fqcn)}: RETURN {report.return_status}", ""]
    if report.inherits_from:
        names = ", ".join(_code(name) for name in report.inherits_from)
        lines += [f"Inherits returns from: {names}", ""]
    if report.error is not None:
        lines += [f"Error: {_escape(report.error)}", ""]
    elif not report.keys:
        lines += ["No return keys found.", ""]
    else:
        lines += [
            _row(("Key", "Status", "Doc", "Code", "Tests")),
            "|---|---|---|---|--:|",
        ]
        for key in report.keys:
            name, status, doc, code, tests = key_cells(key, root)
            code = code if code == "-" else _code(code)
            lines.append(_row((_code(name), status, _escape(doc), code, tests)))
        lines.append("")
    if report.unresolved:
        lines += ["### Unresolved", ""]
        lines += [
            f"- {_code(f'{relative(u.file, root)}:{u.line}')}: {_escape(u.reason)}"
            for u in report.unresolved
        ]
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _row(cells: Sequence[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _code(text: str) -> str:
    return f"`{_escape(text)}`"
