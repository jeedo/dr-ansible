"""Find the keys a module returns through ``exit_json`` (FR-8, FR-9).

The module is parsed with ``ast`` and never imported or run. For every
``<anything>.exit_json(...)`` call, keyword arguments are keys, and a
``**name`` argument is traced back through how ``name`` was built in the same
function: dict literals, ``dict(...)``, ``name[key] = ...``,
``name.update(...)`` and ``name.setdefault(...)``. A keyword whose value is a
dict built the same way also yields its nested keys (``info.size``).

Tracing is flow-insensitive: every contribution in the function counts, so a
key set on any path is reported rather than missed. What cannot be named
statically (computed keys, ``**`` from parameters, helper results) is left
for the unresolved and helper handling in plan task 14.
"""

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from dr_ansible.model import Outcome, ReturnType, StaticKey, Unresolved
from dr_ansible.static.infer import condition_for, infer, parent_map

_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_NESTED_SCOPES = (*_SCOPES, ast.ClassDef)


class AnalysisError(Exception):
    """The file cannot be read or is not valid Python (NFR-8)."""


@dataclass(frozen=True, slots=True)
class ModuleAnalysis:
    """Static evidence from one module file."""

    keys: tuple[StaticKey, ...] = ()
    unresolved: tuple[Unresolved, ...] = ()


@dataclass(frozen=True, slots=True)
class _Entry:
    """One place a key is put into a traced dict."""

    key: str
    value: ast.expr | None
    line: int
    where: ast.AST  # the node whose enclosing conditions apply


def analyze_module(path: Path) -> ModuleAnalysis:
    """Collect the keys ``path`` passes to ``exit_json``, sorted by key and line."""
    tree = _parse(path)
    parents = parent_map(tree)
    tracers: dict[ast.AST, _Tracer] = {}
    keys: set[StaticKey] = set()

    for call in _method_calls(tree, "exit_json"):
        scope = _scope_of(call, parents)
        tracer = tracers.setdefault(scope, _Tracer(path, scope, parents))
        condition = condition_for(call, parents)
        for keyword in call.keywords:
            if keyword.arg is None:
                if isinstance(keyword.value, ast.Name):
                    name = keyword.value.id
                    keys.update(tracer.dict_keys(name, "", frozenset({name})))
                continue
            keys.update(
                tracer.value_keys(
                    keyword.arg, keyword.value, keyword.lineno, condition, frozenset()
                )
            )

    ordered = sorted(
        keys, key=lambda k: (k.path, k.line, k.condition or "", k.inferred_type or "")
    )
    return ModuleAnalysis(keys=tuple(ordered))


class _Tracer:
    """Answers questions about names within one function (or the module body)."""

    def __init__(self, path: Path, scope: ast.AST, parents: dict[ast.AST, ast.AST]):
        self._path = path
        self._parents = parents
        self._nodes = list(_local_nodes(scope))
        self._entries: dict[str, list[_Entry]] = {}
        self._dict_names: set[str] = set()
        self._assigned: dict[str, list[ast.expr]] = {}
        self._index()

    # -- building the index ------------------------------------------------------

    def _index(self) -> None:
        for node in self._nodes:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    self._index_assignment(target, node.value, node)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                self._index_assignment(node.target, node.value, node)
            elif isinstance(node, ast.AugAssign):
                self._index_subscript(node.target, node.value, node)
            elif isinstance(node, ast.Call):
                self._index_method_call(node)

    def _index_assignment(
        self, target: ast.expr, value: ast.expr, stmt: ast.AST
    ) -> None:
        if isinstance(target, ast.Name):
            self._assigned.setdefault(target.id, []).append(value)
            literal = _literal_entries(value, stmt)
            if literal is not None:
                self._dict_names.add(target.id)
                self._entries.setdefault(target.id, []).extend(literal)
        else:
            self._index_subscript(target, value, stmt)

    def _index_subscript(
        self, target: ast.expr, value: ast.expr, stmt: ast.AST
    ) -> None:
        if not (
            isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
        ):
            return
        key = _constant_key(target.slice)
        if key is None:
            return  # computed key: unresolved (plan task 14)
        name = target.value.id
        self._dict_names.add(name)
        self._entries.setdefault(name, []).append(
            _Entry(key, value, target.lineno, stmt)
        )

    def _index_method_call(self, call: ast.Call) -> None:
        func = call.func
        if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
            return
        name = func.value.id
        found: list[_Entry] = []
        if func.attr == "update":
            for arg in call.args:
                found.extend(_literal_entries(arg, call) or [])
            found.extend(
                _Entry(k.arg, k.value, k.lineno, call)
                for k in call.keywords
                if k.arg is not None and _usable_key(k.arg)
            )
        elif func.attr == "setdefault" and call.args:
            key = _constant_key(call.args[0])
            if key is not None:
                default = call.args[1] if len(call.args) > 1 else None
                found.append(_Entry(key, default, call.lineno, call))
        else:
            return
        self._dict_names.add(name)
        self._entries.setdefault(name, []).extend(found)

    # -- queries -------------------------------------------------------------------

    def dict_keys(
        self, name: str, prefix: str, visiting: frozenset[str]
    ) -> list[StaticKey]:
        """Keys put into the dict ``name``, as ``prefix + key`` paths."""
        keys: list[StaticKey] = []
        for entry in self._entries.get(name, []):
            condition = condition_for(entry.where, self._parents)
            keys.extend(
                self.value_keys(
                    prefix + entry.key, entry.value, entry.line, condition, visiting
                )
            )
        return keys

    def value_keys(
        self,
        path: str,
        value: ast.expr | None,
        line: int,
        condition: str | None,
        visiting: frozenset[str],
    ) -> list[StaticKey]:
        """The key at ``path`` set to ``value``, plus any nested dict keys."""
        if value is None:
            return [self._key(path, line, None, condition)]

        if isinstance(value, ast.Name) and value.id in self._dict_names:
            here = [self._key(path, line, ReturnType.DICT, condition)]
            if value.id in visiting:
                return here  # a dict that contains itself: stop
            nested = self.dict_keys(value.id, f"{path}.", visiting | {value.id})
            return here + nested

        literal = _literal_entries(value, value)
        if literal is not None:
            keys = [self._key(path, line, ReturnType.DICT, condition)]
            for entry in literal:
                keys.extend(
                    self.value_keys(
                        f"{path}.{entry.key}",
                        entry.value,
                        entry.line,
                        condition,
                        visiting,
                    )
                )
            return keys

        if isinstance(value, ast.Name):
            return [self._key(path, line, self._name_type(value.id), condition)]
        return [self._key(path, line, infer(value).type, condition)]

    def _name_type(self, name: str) -> ReturnType | None:
        """The type of a plain variable, if every assignment to it agrees."""
        types = {infer(v).type for v in self._assigned.get(name, [])}
        return types.pop() if len(types) == 1 else None

    def _key(
        self, path: str, line: int, type_: ReturnType | None, condition: str | None
    ) -> StaticKey:
        return StaticKey(
            path=path,
            file=self._path,
            line=line,
            outcome=Outcome.SUCCESS,
            inferred_type=type_,
            condition=condition,
        )


# --- helpers --------------------------------------------------------------------------


def _parse(path: Path) -> ast.Module:
    try:
        source = path.read_text()
    except OSError as exc:
        raise AnalysisError(f"{path}: cannot read: {exc}") from exc
    try:
        return ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise AnalysisError(
            f"{path}: SyntaxError: {exc.msg} (line {exc.lineno})"
        ) from exc


def _method_calls(tree: ast.AST, method: str) -> Iterator[ast.Call]:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == method
        ):
            yield node


def _scope_of(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST:
    """The innermost function containing ``node``, or the module."""
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, _SCOPES):
            return current
    return current


def _local_nodes(scope: ast.AST) -> Iterator[ast.AST]:
    """Every node in ``scope`` except those inside nested functions or classes."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _NESTED_SCOPES):
            stack.extend(ast.iter_child_nodes(node))


def _literal_entries(value: ast.expr, where: ast.AST) -> list[_Entry] | None:
    """Entries of a dict literal or ``dict(...)`` call; ``None`` if it is neither."""
    if isinstance(value, ast.Dict):
        return [
            _Entry(key, item, key_node.lineno, where)
            for key_node, item in zip(value.keys, value.values, strict=True)
            if key_node is not None and (key := _constant_key(key_node)) is not None
        ]
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "dict"
    ):
        return [
            _Entry(k.arg, k.value, k.lineno, where)
            for k in value.keywords
            if k.arg is not None and _usable_key(k.arg)
        ]
    return None


def _constant_key(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value if _usable_key(node.value) else None
    return None


def _usable_key(key: str) -> bool:
    """Keys that can be a dotted path segment: non-empty and without a dot."""
    return bool(key) and "." not in key
