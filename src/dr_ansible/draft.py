"""Draft a ``RETURN`` block from the evidence (FR-19, FR-21).

Every non-stale key becomes an entry that follows validate-modules'
``return_schema``:

- ``description``: always the marker :data:`MARKER`; a human writes every
  description (FR-21);
- ``returned``: ``always`` (both success and failure paths), ``failure``
  (failure paths only), ``changed``, ``when <condition>`` (one enclosing
  condition) or ``success``; keys seen only in tests use their result states;
- ``type``: runtime-observed types first, then the type inferred from code,
  then the documented type; ``complex`` when the key has nested keys, ``raw``
  when the evidence is missing or conflicts;
- ``elements``: for lists, from the sample or the documentation;
- ``sample``: from tests, else the first literal in the code, redacted
  (FR-16), and left out if neither exists or the key has nested keys;
- ``contains``: nested keys, with missing parents created as ``complex``.

Entries are sorted by key, and each carries a comment naming its evidence,
for example ``# from plugins/action/fetch.py:199,208; seen in 4 tests``.
"""

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml

from dr_ansible.config import Config
from dr_ansible.mining.redact import redact_sample
from dr_ansible.model import (
    DocResult,
    JSONValue,
    KeyReport,
    KeyStatus,
    Location,
    ModuleReport,
    Outcome,
    ResultState,
    ReturnStatus,
    ReturnType,
    Sample,
)

#: The description of every drafted key, for a human to replace (FR-21).
MARKER = "DR-ANSIBLE-TODO"
_INDENT = "    "
_PYTHON_TYPES = {
    "bool": ReturnType.BOOL,
    "int": ReturnType.INT,
    "float": ReturnType.FLOAT,
    "str": ReturnType.STR,
    "list": ReturnType.LIST,
    "dict": ReturnType.DICT,
}
_PLAIN = re.compile(r"[A-Za-z0-9_/](?:[A-Za-z0-9_./ -]*[A-Za-z0-9_./-])?")


@dataclass(frozen=True, slots=True)
class DraftEntry:
    """One drafted ``RETURN`` entry and the evidence behind it."""

    name: str
    path: str
    returned: str
    type: str
    elements: str | None = None
    sample: Sample | None = None
    sources: tuple[Location, ...] = ()
    seen: int = 0
    contains: tuple["DraftEntry", ...] = ()


def build_entries(keys: Iterable[KeyReport], config: Config) -> tuple[DraftEntry, ...]:
    """The draft's top-level entries, with nested keys under ``contains``."""
    reports = {k.name: k for k in keys if k.status is not KeyStatus.STALE}
    paths = set(reports)
    for path in list(paths):
        parts = path.split(".")
        paths.update(".".join(parts[:depth]) for depth in range(1, len(parts)))

    children: dict[str, list[str]] = {}
    for path in paths:
        if "." in path:
            children.setdefault(path.rsplit(".", 1)[0], []).append(path)

    def make(path: str) -> DraftEntry:
        nested = tuple(make(child) for child in sorted(children.get(path, [])))
        return _entry(path, reports.get(path), nested, config)

    return tuple(make(path) for path in sorted(p for p in paths if "." not in p))


def render_draft(report: ModuleReport, config: Config, root: Path) -> str:
    """The draft as Python source: a header comment and a ``RETURN = r'''...'''``."""
    header = [
        f"# dr-ansible draft for {report.fqcn}: replace every {MARKER} with a"
        " description."
    ]
    header += _unresolved_lines(report, root)
    entries = build_entries(report.keys, config)
    if not entries:
        header.append("# no return keys were found")
        return "\n".join([*header, 'RETURN = r"""#"""', ""])

    body: list[str] = []
    for entry in entries:
        body.extend(_render(entry, "", root))
    return "\n".join([*header, 'RETURN = r"""', *body, '"""', ""])


def render_merge(
    report: ModuleReport, docs: DocResult, config: Config, root: Path
) -> str:
    """Merge mode (FR-20, AC-9): keep the existing ``RETURN``, add what is missing.

    The existing ``RETURN`` text is copied byte for byte, and only missing
    top-level keys are appended. Missing keys nested under an existing entry
    are listed as YAML comments, never edited in. Docs in a sidecar ``.yml``
    get just the entries to paste under its ``RETURN:`` key. Without a valid
    existing ``RETURN`` to keep, the result is the full draft, with a note.
    """
    sidecar = report.module.sidecar_path
    if sidecar is None and (
        docs.raw_text is None or docs.status is ReturnStatus.INVALID
    ):
        note = (
            "# the existing RETURN is not valid YAML: fix it, or replace it with"
            " this full draft"
            if docs.status is ReturnStatus.INVALID
            else "# no existing RETURN to merge into: this is a full draft"
        )
        full = render_draft(report, config, root).split("\n")
        return "\n".join([full[0], note, *full[1:]])

    documented = {d.path for d in docs.keys}
    missing = [
        k
        for k in report.keys
        if k.documented is None and k.status is not KeyStatus.STALE
    ]
    top_level = [k for k in missing if k.name.split(".", 1)[0] not in documented]
    nested = [k for k in missing if k.name.split(".", 1)[0] in documented]

    header = [
        f"# dr-ansible merge draft for {report.fqcn}: existing entries are"
        f" unchanged; replace every {MARKER} with a description."
    ]
    header += _unresolved_lines(report, root)
    if sidecar is not None:
        header.append(_sidecar_note(sidecar, docs, root))
    if not missing:
        header.append("# nothing to add")

    base = "  " if sidecar is not None else ""
    added: list[str] = []
    for entry in build_entries(top_level, config):
        added.extend(_render(entry, base, root))
    added.extend(_nested_comments(nested, documented, config, root))

    if sidecar is not None:
        return "\n".join([*header, *added, ""])
    existing = docs.raw_text or ""
    if added and existing and not existing.endswith("\n"):
        existing += "\n"
    body = existing + "".join(f"{line}\n" for line in added)
    return "\n".join([*header, f'RETURN = r"""{body}"""', ""])


def _unresolved_lines(report: ModuleReport, root: Path) -> list[str]:
    return [
        f"# unresolved: {_relative(u.file, root)}:{u.line}: {u.reason}"
        for u in report.unresolved
    ]


def _sidecar_note(sidecar: Path, docs: DocResult, root: Path) -> str:
    if docs.status is ReturnStatus.MISSING:
        return f"# {sidecar.name} has no RETURN: key; add one with these entries"
    if docs.status is ReturnStatus.INVALID:
        return f"# {_relative(sidecar, root)} is not valid YAML; fix it first"
    return f"# add these entries under RETURN: in {_relative(sidecar, root)}"


def _nested_comments(
    keys: list[KeyReport], documented: set[str], config: Config, root: Path
) -> list[str]:
    """One comment line per missing key nested under an existing entry."""
    if not keys:
        return []
    lines = [
        "# dr-ansible: keys missing under existing entries"
        " (add them to the entry's contains):"
    ]

    def walk(entries: tuple[DraftEntry, ...]) -> None:
        for entry in entries:
            if entry.path not in documented:
                line = f"#   {entry.path}: type {entry.type}, returned {entry.returned}"
                evidence = _evidence(entry, root)
                lines.append(f"{line}; {evidence}" if evidence else line)
            walk(entry.contains)

    walk(build_entries(keys, config))
    return lines


# --- building one entry -------------------------------------------------------------


def _entry(
    path: str,
    key: KeyReport | None,
    contains: tuple[DraftEntry, ...],
    config: Config,
) -> DraftEntry:
    name = path.rsplit(".", 1)[-1]
    if key is None:  # a parent created for nested keys
        return DraftEntry(
            name=name,
            path=path,
            returned="success",
            type=ReturnType.COMPLEX.value,
            contains=contains,
        )

    type_ = ReturnType.COMPLEX.value if contains else _type(key)
    sample = None if contains else _sample(key, config)
    elements = _elements(key, sample) if type_ == ReturnType.LIST.value else None
    return DraftEntry(
        name=name,
        path=path,
        returned=_returned(key),
        type=type_,
        elements=elements,
        sample=sample,
        sources=tuple(sorted(Location(s.file, s.line) for s in key.static)),
        seen=key.observed.count if key.observed is not None else 0,
        contains=contains,
    )


def _returned(key: KeyReport) -> str:
    if not key.static:
        results = key.observed.results if key.observed is not None else frozenset()
        if results == {ResultState.FAILED}:
            return "failure"
        if results == {ResultState.CHANGED}:
            return "changed"
        return "success"

    success = [s for s in key.static if s.outcome is Outcome.SUCCESS]
    failure = [s for s in key.static if s.outcome is Outcome.FAILURE]
    if not success:
        return "failure"
    if failure:
        unconditional = all(
            any(s.condition is None for s in group) for group in (success, failure)
        )
        return "always" if unconditional else "success"
    conditions = {s.condition for s in success}
    if None in conditions or len(conditions) != 1:
        return "success"
    (condition,) = conditions
    assert condition is not None
    return "changed" if condition == "changed" else f"when {condition}"


def _type(key: KeyReport) -> str:
    if key.observed is not None:
        observed = {_PYTHON_TYPES[t] for t in key.observed.types if t in _PYTHON_TYPES}
        if observed:
            return _one_type(observed)
    inferred = {s.inferred_type for s in key.static if s.inferred_type is not None}
    if inferred:
        return _one_type(inferred)
    documented = key.documented.type if key.documented is not None else None
    if documented == ReturnType.COMPLEX.value:
        return ReturnType.DICT.value  # complex needs contains, which it lacks here
    if documented is not None and documented in ReturnType:
        return documented
    return ReturnType.RAW.value


def _one_type(types: set[ReturnType]) -> str:
    if types == {ReturnType.INT, ReturnType.FLOAT}:
        return ReturnType.FLOAT.value
    if len(types) == 1:
        return next(iter(types)).value
    return ReturnType.RAW.value


def _elements(key: KeyReport, sample: Sample | None) -> str | None:
    if sample is not None and isinstance(sample.value, list) and sample.value:
        types = {_json_type(item) for item in sample.value}
        if None not in types:
            chosen = _one_type({t for t in types if t is not None})
            if chosen != ReturnType.RAW.value:
                return chosen
        return None
    documented = key.documented.elements if key.documented is not None else None
    return documented if documented is not None and documented in ReturnType else None


def _json_type(value: JSONValue) -> ReturnType | None:
    # bool before int: True is an int in Python.
    for kind, type_ in (
        (bool, ReturnType.BOOL),
        (int, ReturnType.INT),
        (float, ReturnType.FLOAT),
        (str, ReturnType.STR),
        (list, ReturnType.LIST),
        (dict, ReturnType.DICT),
    ):
        if isinstance(value, kind):
            return type_
    return None


def _sample(key: KeyReport, config: Config) -> Sample | None:
    if key.observed is not None and key.observed.sample is not None:
        return redact_sample(key.name, key.observed.sample.value, config)
    literals = [
        s for s in sorted(key.static, key=lambda s: (s.file, s.line)) if s.literal
    ]
    if literals and literals[0].literal is not None:
        return redact_sample(key.name, literals[0].literal.value, config)
    return None


# --- rendering ----------------------------------------------------------------------


def _render(entry: DraftEntry, indent: str, root: Path) -> list[str]:
    inner = indent + _INDENT
    lines = [f"{indent}{_scalar(entry.name)}:"]
    evidence = _evidence(entry, root)
    if evidence:
        lines.append(f"{inner}# {evidence}")
    lines.append(f"{inner}description: {MARKER}")
    lines.append(f"{inner}returned: {_scalar(entry.returned)}")
    lines.append(f"{inner}type: {entry.type}")
    if entry.elements is not None:
        lines.append(f"{inner}elements: {entry.elements}")
    if entry.sample is not None:
        lines.append(f"{inner}sample: {_scalar(entry.sample.value)}")
    if entry.contains:
        lines.append(f"{inner}contains:")
        for child in entry.contains:
            lines.extend(_render(child, inner + _INDENT, root))
    return lines


def _evidence(entry: DraftEntry, root: Path) -> str:
    parts = []
    if entry.sources:
        by_file: dict[Path, list[int]] = {}
        for source in entry.sources:
            by_file.setdefault(source.file, []).append(source.line)
        files = [
            f"{_relative(file, root)}:{','.join(str(n) for n in sorted(set(lines)))}"
            for file, lines in sorted(by_file.items())
        ]
        parts.append("from " + " and ".join(files))
    if entry.seen:
        parts.append(f"seen in {entry.seen} test{'' if entry.seen == 1 else 's'}")
    return "; ".join(parts)


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _scalar(value: JSONValue) -> str:
    """``value`` as a one-line YAML scalar or flow collection."""
    if isinstance(value, str) and _PLAIN.fullmatch(value):
        try:
            if yaml.safe_load(value) == value:
                return value
        except yaml.YAMLError:
            pass
    # JSON is valid YAML, and its strings are always safely quoted.
    return json.dumps(value)
