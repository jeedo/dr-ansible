"""Classify a module's ``RETURN`` documentation (FR-5, FR-6).

Each module gets one status: ``missing``, ``placeholder``, ``invalid``,
``present`` or ``exempt``. Parsing reuses ansible-core's
``read_docstring``, but that cannot tell a missing ``RETURN`` from a
placeholder (both give ``None``), and it raises if any part of the docs is
broken. So an ``ast`` pass first finds the ``RETURN`` assignment and its exact
text, and when ``read_docstring`` fails the ``RETURN`` text is checked on its
own. Module code is parsed, never imported or run.
"""

import ast
from pathlib import Path

import yaml
from ansible.errors import AnsibleParserError
from ansible.parsing.plugin_docs import read_docstring
from ansible.parsing.yaml.loader import AnsibleLoader

from dr_ansible.config import Config
from dr_ansible.model import DocResult, Language, ModuleInfo, ReturnStatus

_EXEMPTABLE = frozenset({ReturnStatus.MISSING, ReturnStatus.PLACEHOLDER})


class DocsAuditError(Exception):
    """The module file cannot be read or is not valid Python (NFR-8)."""


def audit_docs(module: ModuleInfo, config: Config) -> DocResult:
    """Give ``module`` its ``RETURN`` status.

    Docs come from the sidecar ``.yml`` when there is one, as in ansible-core.
    A module on the exempt allowlist (by any of its names) is ``exempt``
    instead of ``missing`` or ``placeholder``; an exempt module that does
    document its returns is still checked.
    """
    if module.language is not Language.PYTHON:
        raise ValueError(f"{module.fqcn}: PowerShell modules are not audited")

    if module.sidecar_path is not None:
        status, raw_text, problems = _from_sidecar(module.sidecar_path)
    else:
        status, raw_text, problems = _from_python(module.module_path)

    if status in _EXEMPTABLE and set(module.names) & config.exempt_modules:
        status = ReturnStatus.EXEMPT
    return DocResult(status=status, raw_text=raw_text, problems=tuple(problems))


def _from_python(path: Path) -> tuple[ReturnStatus, str | None, list[str]]:
    source = _read(path)
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise DocsAuditError(
            f"{path}: SyntaxError: {exc.msg} (line {exc.lineno})"
        ) from exc

    value = _return_value(tree)
    if value is _NOT_LITERAL:
        return ReturnStatus.INVALID, None, ["RETURN is not a plain string literal"]
    raw_text = value if isinstance(value, str) else None

    try:
        returndocs = _read_docstring(path)
    except AnsibleParserError as exc:
        return _fallback(
            raw_text, f"other documentation failed to parse: {_first_line(exc)}"
        )

    if raw_text is None:
        problems = []
        if _has_annotated_return(tree):
            problems.append(
                "RETURN uses an annotated assignment, which ansible-core ignores"
            )
        return ReturnStatus.MISSING, None, problems
    status, problems = _classify(returndocs)
    return status, raw_text, problems


def _fallback(
    raw_text: str | None, docs_problem: str
) -> tuple[ReturnStatus, str | None, list[str]]:
    """``read_docstring`` failed: judge the ``RETURN`` text on its own."""
    if raw_text is None:
        return ReturnStatus.MISSING, None, [docs_problem]
    try:
        parsed = _load_yaml(raw_text)
    except yaml.YAMLError as exc:
        return (
            ReturnStatus.INVALID,
            raw_text,
            [f"RETURN is not valid YAML: {_first_line(exc)}"],
        )
    status, problems = _classify(parsed)
    return status, raw_text, [*problems, docs_problem]


def _from_sidecar(path: Path) -> tuple[ReturnStatus, str | None, list[str]]:
    try:
        data = _load_yaml(_read(path))
    except yaml.YAMLError as exc:
        return (
            ReturnStatus.INVALID,
            None,
            [f"sidecar is not valid YAML: {_first_line(exc)}"],
        )
    if not isinstance(data, dict):
        return ReturnStatus.INVALID, None, ["sidecar must be a YAML mapping"]
    if "RETURN" not in data:
        return ReturnStatus.MISSING, None, []

    try:
        returndocs = _read_docstring(path)
    except AnsibleParserError as exc:
        status, problems = _classify(data["RETURN"])
        problems.append(f"other documentation failed to parse: {_first_line(exc)}")
        return status, None, problems
    status, problems = _classify(returndocs)
    return status, None, problems


def _classify(returndocs: object) -> tuple[ReturnStatus, list[str]]:
    if returndocs is None or returndocs == {}:
        return ReturnStatus.PLACEHOLDER, []
    if isinstance(returndocs, dict):
        return ReturnStatus.PRESENT, []
    return ReturnStatus.INVALID, [
        f"RETURN must be a YAML mapping, got {_type_name(returndocs)}"
    ]


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
