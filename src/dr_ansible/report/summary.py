"""Per-module counts and helpers shared by the renderers."""

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from dr_ansible.model import KeyReport, KeyStatus, ModuleReport, StaticKey

#: Statuses counted as undocumented: found in code, or only in tests.
_UNDOCUMENTED = frozenset({KeyStatus.UNDOCUMENTED, KeyStatus.TEST_ONLY})


@dataclass(frozen=True, slots=True)
class Counts:
    """How many of a module's keys each source knows about, and how many disagree."""

    documented: int
    code: int
    tests: int
    undocumented: int
    stale: int


def counts(report: ModuleReport) -> Counts:
    """The summary counts for ``report``'s keys."""
    keys = report.keys
    return Counts(
        documented=sum(k.documented is not None for k in keys),
        code=sum(bool(k.static) for k in keys),
        tests=sum(k.observed is not None for k in keys),
        undocumented=sum(k.status in _UNDOCUMENTED for k in keys),
        stale=sum(k.status is KeyStatus.STALE for k in keys),
    )


def by_fqcn(reports: Iterable[ModuleReport]) -> list[ModuleReport]:
    return sorted(reports, key=lambda r: r.fqcn)


def relative(path: Path, root: Path) -> str:
    """``path`` relative to ``root``, or absolute when it lies outside it."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def code_locations(static: Iterable[StaticKey], root: Path) -> str:
    """Where the code sets a key: ``file:line,line`` per file, comma-joined."""
    lines: dict[str, list[int]] = {}
    for key in static:
        found = lines.setdefault(relative(key.file, root), [])
        if key.line not in found:
            found.append(key.line)
    return ", ".join(
        f"{file}:{','.join(str(n) for n in sorted(numbers))}"
        for file, numbers in sorted(lines.items())
    )


def key_cells(key: KeyReport, root: Path) -> tuple[str, str, str, str, str]:
    """A key's KEY, STATUS, DOC, CODE and TESTS cells; ``-`` for a silent source."""
    documented = key.documented
    doc = (documented.type or "?") if documented is not None else "-"
    code = code_locations(key.static, root) or "-"
    tests = str(key.observed.count) if key.observed is not None else "-"
    return key.name, str(key.status), doc, code, tests
