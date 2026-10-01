"""Find return keys that a module's integration tests read (FR-13, AC-5).

Every YAML file under the module's integration target is composed with
PyYAML (keeping line numbers, and without needing to understand Ansible tags
such as ``!unsafe`` or ``!vault``). Any mapping with a key that is one of the
module's names, or an ``action``/``local_action`` naming it, is a task calling
the module; its ``register:`` variable is recorded. References to those
variables (``var.key``, ``var['key']``, ``var.get('key')``) are then
collected from ``assert`` (``that`` and its messages), ``when``,
``failed_when``, ``changed_when``, ``until`` and ``debug`` (``var`` and
``msg``). Each referenced key, and each parent of a nested key, becomes an
:class:`~dr_ansible.model.Observation` with its count and source lines.

Static mining sees only names, so observations carry no types, result
states or samples; runtime mining (``--run``) adds those.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from dr_ansible.model import Location, ModuleInfo, Observation

_YAML_SUFFIXES = frozenset({".yml", ".yaml"})
#: Task fields holding bare Jinja expressions or templates that may read results.
_EXPRESSION_FIELDS = frozenset({"when", "failed_when", "changed_when", "until"})
_ASSERT_FIELDS = frozenset({"that", "fail_msg", "success_msg", "msg"})
_DEBUG_FIELDS = frozenset({"var", "msg"})
_ACTION_FIELDS = ("action", "local_action")

_IDENT = re.compile(r"[A-Za-z_]\w*")
_QUOTED_INDEX = re.compile(r"""\[\s*(['"])([^'"]*)\1\s*\]""")
_GET_ARG = re.compile(r"""\(\s*(['"])([^'"]*)\1""")


@dataclass(frozen=True, slots=True)
class Registration:
    """A task calling the module (as ``invoked_as``) that registers ``variable``."""

    variable: str
    invoked_as: str
    location: Location


@dataclass(frozen=True, slots=True)
class MiningResult:
    """What static mining found for one module."""

    observations: tuple[Observation, ...] = ()
    registrations: tuple[Registration, ...] = ()
    problems: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Text:
    """A scalar that may reference registered variables."""

    value: str
    location: Location
    block: bool  # a | or > block scalar: content starts on the next line


def mine_target(module: ModuleInfo) -> MiningResult:
    """Mine the module's integration target, if it has one."""
    if module.test_target is None or not module.test_target.is_dir():
        return MiningResult()

    names = frozenset(module.names)
    registrations: list[Registration] = []
    texts: list[_Text] = []
    problems: list[str] = []

    for path in _yaml_files(module.test_target):
        try:
            documents = list(yaml.compose_all(path.read_text(), Loader=yaml.SafeLoader))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            problems.append(f"{path}: cannot parse: {_first_line(exc)}")
            continue
        for document in documents:
            if document is not None:
                _walk(document, path, names, registrations, texts)

    variables = {r.variable for r in registrations}
    found: dict[str, list[Location]] = {}
    for text in texts:
        for key_path, location in _references(text, variables):
            found.setdefault(key_path, []).append(location)

    observations = tuple(
        Observation(path=key_path, count=len(sources), sources=tuple(sources))
        for key_path, sources in sorted(found.items())
    )
    ordered = tuple(sorted(registrations, key=lambda r: (r.location, r.variable)))
    return MiningResult(
        observations=observations, registrations=ordered, problems=tuple(problems)
    )


# --- walking the YAML ---------------------------------------------------------------


def _yaml_files(target: Path) -> list[Path]:
    return sorted(
        p for p in target.rglob("*") if p.is_file() and p.suffix in _YAML_SUFFIXES
    )


def _walk(
    node: Node,
    path: Path,
    names: frozenset[str],
    registrations: list[Registration],
    texts: list[_Text],
) -> None:
    if isinstance(node, SequenceNode):
        for item in node.value:
            _walk(item, path, names, registrations, texts)
        return
    if not isinstance(node, MappingNode):
        return

    fields = {
        key.value: value
        for key, value in node.value
        if isinstance(key, ScalarNode) and isinstance(key.value, str)
    }
    invoked_as = _invoked_module(fields, names)
    register = fields.get("register")
    if invoked_as is not None and isinstance(register, ScalarNode) and register.value:
        registrations.append(
            Registration(register.value, invoked_as, _location(path, register))
        )

    for name, value in fields.items():
        if name in _EXPRESSION_FIELDS:
            texts.extend(_scalars(value, path))
        elif _short(name) == "assert" and isinstance(value, MappingNode):
            texts.extend(_sub_fields(value, _ASSERT_FIELDS, path))
        elif _short(name) == "debug" and isinstance(value, MappingNode):
            texts.extend(_sub_fields(value, _DEBUG_FIELDS, path))

    for _, value in node.value:
        _walk(value, path, names, registrations, texts)


def _invoked_module(fields: dict[str, Node], names: frozenset[str]) -> str | None:
    """The name this task calls the module by, if it calls the module."""
    for name in fields:
        if name in names:
            return name
    for field in _ACTION_FIELDS:
        value = fields.get(field)
        called: str | None = None
        if (
            isinstance(value, ScalarNode)
            and isinstance(value.value, str)
            and value.value
        ):
            called = value.value.split()[0]
        elif isinstance(value, MappingNode):
            module = {
                k.value: v.value
                for k, v in value.value
                if isinstance(k, ScalarNode) and isinstance(v, ScalarNode)
            }.get("module")
            called = module if isinstance(module, str) else None
        if called in names:
            return called
    return None


def _sub_fields(node: MappingNode, wanted: frozenset[str], path: Path) -> list[_Text]:
    texts: list[_Text] = []
    for key, value in node.value:
        if isinstance(key, ScalarNode) and key.value in wanted:
            texts.extend(_scalars(value, path))
    return texts


def _scalars(node: Node, path: Path) -> Iterator[_Text]:
    """String scalars in ``node``, which is a scalar or a list of them."""
    items = node.value if isinstance(node, SequenceNode) else [node]
    for item in items:
        if isinstance(item, ScalarNode) and isinstance(item.value, str):
            yield _Text(item.value, _location(path, item), item.style in ("|", ">"))


def _short(name: str) -> str:
    """``ansible.builtin.assert`` -> ``assert``."""
    return name.rsplit(".", 1)[-1]


def _location(path: Path, node: Node) -> Location:
    return Location(path, node.start_mark.line + 1)


# --- finding references in expressions ----------------------------------------------


def _references(text: _Text, variables: set[str]) -> Iterator[tuple[str, Location]]:
    """Each key path read from a registered variable in ``text``, with its line."""
    for variable in sorted(variables):
        pattern = re.compile(rf"(?<![\w.]){re.escape(variable)}(?=[.\[])")
        for match in pattern.finditer(text.value):
            segments = _segments(text.value, match.end())
            if not segments:
                continue
            offset = text.value.count("\n", 0, match.start()) + (1 if text.block else 0)
            location = Location(text.location.file, text.location.line + offset)
            for depth in range(1, len(segments) + 1):
                yield ".".join(segments[:depth]), location


def _segments(text: str, index: int) -> list[str]:
    """Key names read at ``text[index:]``: ``.a``, ``['a']`` and ``.get('a')``.

    A method call (other than ``get``) or a non-string index ends the path.
    """
    segments: list[str] = []
    while index < len(text):
        if text[index] == ".":
            ident = _IDENT.match(text, index + 1)
            if ident is None:
                break
            end = ident.end()
            if end < len(text) and text[end] == "(":
                if ident.group() == "get":
                    argument = _GET_ARG.match(text, end)
                    if argument is not None:
                        segments.append(argument.group(2))
                break
            segments.append(ident.group())
            index = end
        elif text[index] == "[":
            quoted = _QUOTED_INDEX.match(text, index)
            if quoted is None:
                break
            segments.append(quoted.group(2))
            index = quoted.end()
        else:
            break
    # Keys that cannot be dotted path segments end the path.
    usable: list[str] = []
    for segment in segments:
        if not segment or "." in segment:
            break
        usable.append(segment)
    return usable


def _first_line(exc: Exception) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__
