"""Audit a module's ``RETURN`` documentation (FR-5 to FR-7).

Each module gets one status: ``missing``, ``placeholder``, ``invalid``,
``present`` or ``exempt``. Parsing reuses ansible-core's
``read_docstring``, but that cannot tell a missing ``RETURN`` from a
placeholder (both give ``None``), and it raises if any part of the docs is
broken. So an ``ast`` pass first finds the ``RETURN`` assignment and its exact
text, and when ``read_docstring`` fails the ``RETURN`` text is checked on its
own. Module code is parsed, never imported or run.

For ``present`` docs, every documented key is listed, nested ``contains``
keys as dotted paths, and missing or malformed fields are reported as
problems.
"""

import ast
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from ansible.errors import AnsibleParserError
from ansible.parsing.plugin_docs import read_docstring
from ansible.parsing.yaml.loader import AnsibleLoader

from dr_ansible.config import Config
from dr_ansible.model import (
    DocResult,
    DocumentedKey,
    Language,
    ModuleInfo,
    ReturnStatus,
    ReturnType,
)

_EXEMPTABLE = frozenset({ReturnStatus.MISSING, ReturnStatus.PLACEHOLDER})
#: Fields every top-level key must have (FR-7, validate-modules' return_schema).
_REQUIRED_TOP_LEVEL = ("description", "returned", "type")
_VALID_TYPES = ", ".join(sorted(t.value for t in ReturnType))


class DocsAuditError(Exception):
    """The module file cannot be read or is not valid Python (NFR-8)."""


@dataclass(slots=True)
class _Outcome:
    status: ReturnStatus
    raw_text: str | None = None
    problems: list[str] = field(default_factory=list)
    keys: list[DocumentedKey] = field(default_factory=list)


def audit_docs(module: ModuleInfo, config: Config) -> DocResult:
    """Give ``module`` its ``RETURN`` status and list its documented keys.

    Docs come from the sidecar ``.yml`` when there is one, as in ansible-core.
    A module on the exempt allowlist (by any of its names) is ``exempt``
    instead of ``missing`` or ``placeholder``; an exempt module that does
    document its returns is still checked.
    """
    if module.language is not Language.PYTHON:
        raise ValueError(f"{module.fqcn}: PowerShell modules are not audited")

    if module.sidecar_path is not None:
        outcome = _from_sidecar(module.sidecar_path)
    else:
        outcome = _from_python(module.module_path)

    if outcome.status in _EXEMPTABLE and set(module.names) & config.exempt_modules:
        outcome.status = ReturnStatus.EXEMPT
    return DocResult(
        status=outcome.status,
        keys=tuple(outcome.keys),
        raw_text=outcome.raw_text,
        problems=tuple(outcome.problems),
    )


# --- finding and parsing RETURN -------------------------------------------------------


def _from_python(path: Path) -> _Outcome:
    source = _read(path)
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise DocsAuditError(
            f"{path}: SyntaxError: {exc.msg} (line {exc.lineno})"
        ) from exc

    value = _return_value(tree)
    if value is _NOT_LITERAL:
        return _Outcome(
            ReturnStatus.INVALID, problems=["RETURN is not a plain string literal"]
        )
    raw_text = value if isinstance(value, str) else None

    try:
        returndocs = _read_docstring(path)
    except AnsibleParserError as exc:
        return _fallback(
            raw_text, f"other documentation failed to parse: {_first_line(exc)}"
        )

    if raw_text is None:
        outcome = _Outcome(ReturnStatus.MISSING)
        if _has_annotated_return(tree):
            outcome.problems.append(
                "RETURN uses an annotated assignment, which ansible-core ignores"
            )
        return outcome
    outcome = _classify(returndocs)
    outcome.raw_text = raw_text
    return outcome


def _fallback(raw_text: str | None, docs_problem: str) -> _Outcome:
    """``read_docstring`` failed: judge the ``RETURN`` text on its own."""
    if raw_text is None:
        return _Outcome(ReturnStatus.MISSING, problems=[docs_problem])
    try:
        parsed = _load_yaml(raw_text)
    except yaml.YAMLError as exc:
        return _Outcome(
            ReturnStatus.INVALID,
            raw_text,
            [f"RETURN is not valid YAML: {_first_line(exc)}"],
        )
    outcome = _classify(parsed)
    outcome.raw_text = raw_text
    outcome.problems.append(docs_problem)
    return outcome


def _from_sidecar(path: Path) -> _Outcome:
    try:
        data = _load_yaml(_read(path))
    except yaml.YAMLError as exc:
        return _Outcome(
            ReturnStatus.INVALID,
            problems=[f"sidecar is not valid YAML: {_first_line(exc)}"],
        )
    if not isinstance(data, dict):
        return _Outcome(
            ReturnStatus.INVALID, problems=["sidecar must be a YAML mapping"]
        )
    if "RETURN" not in data:
        return _Outcome(ReturnStatus.MISSING)

    try:
        returndocs = _read_docstring(path)
    except AnsibleParserError as exc:
        outcome = _classify(data["RETURN"])
        outcome.problems.append(
            f"other documentation failed to parse: {_first_line(exc)}"
        )
        return outcome
    return _classify(returndocs)


def _classify(returndocs: object) -> _Outcome:
    if returndocs is None or returndocs == {}:
        return _Outcome(ReturnStatus.PLACEHOLDER)
    if isinstance(returndocs, dict):
        keys: list[DocumentedKey] = []
        problems: list[str] = []
        _flatten(returndocs, "", keys, problems)
        return _Outcome(ReturnStatus.PRESENT, keys=keys, problems=problems)
    return _Outcome(
        ReturnStatus.INVALID,
        problems=[f"RETURN must be a YAML mapping, got {_type_name(returndocs)}"],
    )


# --- documented keys (FR-7) -----------------------------------------------------------


def _flatten(
    entries: dict[object, object],
    prefix: str,
    keys: list[DocumentedKey],
    problems: list[str],
) -> None:
    """Add each entry, and its ``contains`` entries, as dotted-path keys.

    Keys are visited in sorted order so problems come out deterministically.
    """
    for name in sorted(entries, key=str):
        entry = entries[name]
        path = f"{prefix}{name}"
        if "." in str(name):
            problems.append(f"{path}: key name contains '.'")
            continue
        if not isinstance(entry, dict):
            problems.append(f"{path}: entry must be a mapping, got {_type_name(entry)}")
            continue

        if not prefix:
            for required in _REQUIRED_TOP_LEVEL:
                if not _present(entry.get(required)):
                    problems.append(f"{path}: missing required field '{required}'")
        doc_type = _optional_str(entry.get("type"))
        if doc_type is not None and doc_type not in ReturnType:
            problems.append(f"{path}: type {doc_type!r} is not one of {_VALID_TYPES}")

        keys.append(
            DocumentedKey(
                path=path,
                type=doc_type,
                returned=_optional_str(entry.get("returned")),
                has_description=_present(entry.get("description")),
                elements=_optional_str(entry.get("elements")),
            )
        )

        contains = entry.get("contains")
        if contains is None:
            continue
        if not isinstance(contains, dict):
            problems.append(
                f"{path}: contains must be a mapping, got {_type_name(contains)}"
            )
            continue
        _flatten(contains, f"{path}.", keys, problems)


def _present(value: object) -> bool:
    """Whether a field holds something: a non-empty string or list, or a scalar."""
    if value is None:
        return False
    if isinstance(value, str | list):
        return bool(value)
    return True


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


# --- helpers --------------------------------------------------------------------------


class _NotLiteral:
    """Marker for a ``RETURN`` assigned something other than a string literal."""


_NOT_LITERAL = _NotLiteral()


def _return_value(tree: ast.Module) -> str | _NotLiteral | None:
    """The last module-level ``RETURN`` value, as ``read_docstring`` would see it."""
    value: str | _NotLiteral | None = None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "RETURN" for t in node.targets):
            continue
        assigned = node.value
        if isinstance(assigned, ast.Constant) and isinstance(assigned.value, str):
            value = assigned.value
        else:
            value = _NOT_LITERAL
    return value


def _has_annotated_return(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "RETURN"
        for node in tree.body
    )


def _read_docstring(path: Path) -> object:
    docs = read_docstring(str(path.resolve()), verbose=False, ignore_errors=False)
    return docs.get("returndocs")


def _load_yaml(text: str) -> object:
    return yaml.load(text, Loader=AnsibleLoader)  # Ansible's loader is a safe loader


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError as exc:
        raise DocsAuditError(f"{path}: cannot read: {exc}") from exc


def _first_line(exc: Exception) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


def _type_name(value: object) -> str:
    for name, kind in (("str", str), ("list", list), ("bool", bool), ("int", int)):
        if isinstance(value, kind):
            return name
    return type(value).__name__
