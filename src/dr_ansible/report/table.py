"""Terminal tables: the audit summary and one module's keys (FR-19).

Tables are drawn with ``rich`` when it is installed (the ``rich`` extra) and
as plain aligned text otherwise. Both are returned as strings without colour
codes, so the caller decides where they go.
"""

import importlib.util
from collections.abc import Iterable, Sequence
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING

from dr_ansible.model import ModuleReport
from dr_ansible.report.summary import by_fqcn, counts, key_cells, relative

if TYPE_CHECKING:
    from rich.console import RenderableType

#: Width of the virtual terminal rich tables are drawn for.
RICH_WIDTH = 120

_AUDIT_HEADERS = ("MODULE", "RETURN", "DOC", "CODE", "TESTS", "UNDOC", "STALE")
_KEY_HEADERS = ("KEY", "STATUS", "DOC", "CODE", "TESTS")


def audit_table(reports: Iterable[ModuleReport], use_rich: bool | None = None) -> str:
    """One row per module: its ``RETURN`` status and key counts."""
    rows = []
    for report in by_fqcn(reports):
        c = counts(report)
        numbers = (c.documented, c.code, c.tests, c.undocumented, c.stale)
        rows.append((report.fqcn, str(report.return_status), *map(str, numbers)))
    right = range(2, len(_AUDIT_HEADERS))
    if _rich(use_rich):
        return _render(_rich_table(_AUDIT_HEADERS, rows, right))
    return _plain_table(_AUDIT_HEADERS, rows, right)


def keys_table(report: ModuleReport, root: Path, use_rich: bool | None = None) -> str:
    """One module's keys with their status and evidence, then unresolved entries."""
    header = f"{report.fqcn}: RETURN {report.return_status}\n"
    if report.inherits_from:
        header += f"inherits returns from: {', '.join(report.inherits_from)}\n"
    if report.error is not None:
        return header + f"error: {report.error}\n"
    rows = [key_cells(key, root) for key in report.keys]
    right = (4,)
    if not rows:
        body = "no return keys found\n"
    elif _rich(use_rich):
        body = _render(_rich_table(_KEY_HEADERS, rows, right))
    else:
        body = _plain_table(_KEY_HEADERS, rows, right)
    text = f"{header}\n{body}"
    if report.unresolved:
        text += "\nunresolved:\n" + "".join(
            f"  {relative(u.file, root)}:{u.line}: {u.reason}\n"
            for u in report.unresolved
        )
    return text


def _rich(use_rich: bool | None) -> bool:
    if use_rich is None:
        return importlib.util.find_spec("rich") is not None
    return use_rich


def _plain_table(
    headers: Sequence[str], rows: Sequence[Sequence[str]], right: Iterable[int]
) -> str:
    right = set(right)
    widths = [
        max(len(cell) for cell in column) for column in zip(headers, *rows, strict=True)
    ]

    def line(cells: Sequence[str]) -> str:
        padded = (
            cell.rjust(width) if i in right else cell.ljust(width)
            for i, (cell, width) in enumerate(zip(cells, widths, strict=True))
        )
        return "  ".join(padded).rstrip() + "\n"

    return "".join(line(cells) for cells in [headers, *rows])


def _rich_table(
    headers: Sequence[str], rows: Sequence[Sequence[str]], right: Iterable[int]
) -> "RenderableType":
    from rich.table import Table

    right = set(right)
    table = Table(box=None, header_style="bold", pad_edge=False)
    for i, name in enumerate(headers):
        table.add_column(name, justify="right" if i in right else "left", no_wrap=True)
    for cells in rows:
        table.add_row(*cells)
    return table


def _render(renderable: "RenderableType") -> str:
    from rich.console import Console

    out = StringIO()
    console = Console(
        file=out, width=RICH_WIDTH, color_system=None, force_terminal=False
    )
    console.print(renderable)
    return "\n".join(line.rstrip() for line in out.getvalue().splitlines()) + "\n"
