"""Trace how a returned dict is built, for module and action plugin analysis.

Shared machinery behind :mod:`~dr_ansible.static.module_analyzer` (FR-9) and
:mod:`~dr_ansible.static.action_analyzer` (FR-10). A :class:`Tracer` follows a
mapping back through one function: dict literals (including ``{**other}``),
``dict(...)``, ``name[key] = ...``, ``name.update(...)``,
``name.setdefault(...)``, local helper functions and a wrapper's call sites
(one level deep each). An :class:`Analysis` collects the resulting keys and
unresolved items for one file; subclasses can claim extra calls through
:meth:`Analysis.is_special` and :meth:`Analysis.special`.

Tracing is flow-insensitive: every contribution in the function counts, so a
key set on any path is reported rather than missed. Anything that cannot be
named statically is reported as :class:`~dr_ansible.model.Unresolved` with its
location, never guessed. Code is parsed with ``ast``, never imported or run.
"""

import ast
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from dr_ansible.model import Outcome, ReturnType, Sample, StaticKey, Unresolved
from dr_ansible.static.infer import condition_for, infer, parent_map

_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_NESTED_SCOPES = (*_SCOPES, ast.ClassDef)
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
#: How many helper or wrapper hops are followed (FR-9: "one level deep").
_MAX_DEPTH = 1


class AnalysisError(Exception):
    """The file cannot be read or is not valid Python (NFR-8)."""


# --- shared state -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Ctx:
    """What a key being traced inherits: its outcome and the dicts on the path."""

    outcome: Outcome
    #: The dicts already being traced, as (scope id, name): a name is only the
    #: same dict within one function.
    visiting: frozenset[tuple[int, str]] = frozenset()


@dataclass(frozen=True, slots=True)
class _Entry:
    """One place a key is put into a traced dict."""

    key: str
    value: ast.expr | None
    line: int
    where: ast.AST  # the node whose enclosing conditions apply


@dataclass(frozen=True, slots=True)
class _Merge:
    """A mapping merged into a traced dict: ``update(x)``, ``{**x}``, helper result."""

    value: ast.expr
    where: ast.AST


@dataclass(slots=True)
class _DictInfo:
    """Everything known about how one local name was built as a dict."""

    entries: list[_Entry] = field(default_factory=list)
    merges: list[_Merge] = field(default_factory=list)
    computed: list[int] = field(default_factory=list)  # lines of computed keys


class Analysis:
    def __init__(self, path: Path, tree: ast.Module):
        self.path = path
        self.parents = parent_map(tree)
        self.functions = {
            node.name: node for node in ast.walk(tree) if isinstance(node, _FUNCTIONS)
        }
        self._tree = tree
        self._tracers: dict[tuple[ast.AST, int], Tracer] = {}
        self._keys: set[StaticKey] = set()
        self._unresolved: set[Unresolved] = set()

    def tracer(self, scope: ast.AST, depth: int) -> "Tracer":
        key = (scope, depth)
        if key not in self._tracers:
            self._tracers[key] = Tracer(self, scope, depth)
        return self._tracers[key]

    def add_key(
        self,
        path: str,
        line: int,
        type_: ReturnType | None,
        condition: str | None,
        ctx: Ctx,
        literal: Sample | None = None,
    ) -> None:
        self._keys.add(
            StaticKey(
                path=path,
                file=self.path,
                line=line,
                outcome=ctx.outcome,
                inferred_type=type_,
                condition=condition,
                literal=literal,
            )
        )

    def is_special(self, value: ast.expr, tracer: "Tracer") -> bool:
        """Whether ``value`` is a call a subclass handles itself (see ``special``)."""
        return False

    def special(self, value: ast.expr, tracer: "Tracer", prefix: str, ctx: Ctx) -> None:
        """Handle a mapping that :meth:`is_special` claimed. Subclasses override."""
        raise NotImplementedError

    def add_unresolved(self, line: int, reason: str) -> None:
        self._unresolved.add(Unresolved(file=self.path, line=line, reason=reason))

    def call_sites(self, function: ast.AST) -> list[ast.Call]:
        """Calls of ``function`` by name anywhere in the file, in source order."""
        name = getattr(function, "name", None)
        calls = [
            node
            for node in ast.walk(self._tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == name
        ]
        return sorted(calls, key=lambda c: (c.lineno, c.col_offset))

    def result(self) -> tuple[tuple[StaticKey, ...], tuple[Unresolved, ...]]:
        """Keys and unresolved items, sorted for deterministic output (NFR-6)."""
        keys = sorted(
            self._keys,
            key=lambda k: (
                k.path,
                k.line,
                k.outcome,
                k.condition or "",
                k.inferred_type or "",
            ),
        )
        unresolved = sorted(self._unresolved, key=lambda u: (u.line, u.reason))
        return tuple(keys), tuple(unresolved)


# --- tracing within one scope -------------------------------------------------------


class Tracer:
    """Traces names within one function (or the module body).

    ``depth`` counts helper or wrapper hops already taken; at
    :data:`_MAX_DEPTH` no further hop is followed.
    """

    def __init__(self, analysis: Analysis, scope: ast.AST, depth: int):
        self._a = analysis
        self._scope = scope
        self._depth = depth
        self._dicts: dict[str, _DictInfo] = {}
        self._assigned: dict[str, list[ast.expr]] = {}
        self._kwarg = _kwarg_name(scope)
        self._params = _param_names(scope)
        for node in local_nodes(scope):
            self._index(node)

    # -- indexing ---------------------------------------------------------------------

    def _index(self, node: ast.AST) -> None:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                self._index_assignment(target, node.value, node)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            self._index_assignment(node.target, node.value, node)
        elif isinstance(node, ast.AugAssign):
            self._index_subscript(node.target, node.value, node)
        elif isinstance(node, ast.Call):
            self._index_method_call(node)

    def _info(self, name: str) -> _DictInfo:
        return self._dicts.setdefault(name, _DictInfo())

    def _index_assignment(
        self, target: ast.expr, value: ast.expr, stmt: ast.AST
    ) -> None:
        if not isinstance(target, ast.Name):
            self._index_subscript(target, value, stmt)
            return
        self._assigned.setdefault(target.id, []).append(value)
        if (
            _is_dict_literal(value)
            or self._helper_returning_dict(value)
            or self._a.is_special(value, self)
        ):
            self._info(target.id).merges.append(_Merge(value, stmt))

    def _index_subscript(
        self, target: ast.expr, value: ast.expr, stmt: ast.AST
    ) -> None:
        if not (
            isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
        ):
            return
        info = self._info(target.value.id)
        keys = self._keys(target.slice, stmt)
        if keys is None:
            info.computed.append(target.lineno)
        else:
            info.entries.extend(_Entry(k, value, target.lineno, stmt) for k in keys)

    def _index_method_call(self, call: ast.Call) -> None:
        func = call.func
        if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
            return
        if func.attr == "update":
            info = self._info(func.value.id)
            info.merges.extend(_Merge(arg, call) for arg in call.args)
            for k in call.keywords:
                if k.arg is None:
                    info.merges.append(_Merge(k.value, call))
                elif _usable_key(k.arg):
                    info.entries.append(_Entry(k.arg, k.value, k.lineno, call))
        elif func.attr == "setdefault" and call.args:
            info = self._info(func.value.id)
            keys = self._keys(call.args[0], call)
            if keys is None:
                info.computed.append(call.lineno)
            else:
                default = call.args[1] if len(call.args) > 1 else None
                info.entries.extend(_Entry(k, default, call.lineno, call) for k in keys)

    def _keys(self, node: ast.expr, where: ast.AST) -> list[str] | None:
        """The key ``node`` names: a constant, or each item of a literal loop."""
        key = _constant_key(node)
        if key is not None:
            return [key]
        return _loop_keys(node, where, self._a.parents, self._scope)

    # -- the entry point: an exit_json / fail_json call -------------------------------

    def result_call(self, call: ast.Call, outcome: Outcome) -> None:
        ctx = Ctx(outcome)
        condition = condition_for(call, self._a.parents)
        self.keyword_args(call.keywords, "", condition, ctx)

    def keyword_args(
        self,
        keywords: list[ast.keyword],
        prefix: str,
        condition: str | None,
        ctx: Ctx,
    ) -> None:
        for keyword in keywords:
            if keyword.arg is None:
                self.splat(keyword.value, prefix, ctx)
            elif _usable_key(keyword.arg):
                self.value(
                    prefix + keyword.arg, keyword.value, keyword.lineno, condition, ctx
                )

    # -- splats: **value --------------------------------------------------------------

    def splat(self, value: ast.expr, prefix: str, ctx: Ctx) -> None:
        """Add the keys of the mapping ``value`` (``**value``) under ``prefix``."""
        if isinstance(value, ast.Name):
            name = value.id
            local = name in self._dicts
            if local:
                self.dict_keys(name, prefix, ctx)
            # A **kwargs or parameter dict may also be added to locally: both apply.
            if name == self._kwarg:
                self._wrapper_kwargs(value, prefix, ctx)
            elif name in self._params:
                self._a.add_unresolved(value.lineno, f"**{name} from a parameter")
            elif not local:
                self._unknown(value)
            return
        if _is_dict_literal(value):
            self._literal(value, prefix, None, ctx)
            return
        if self._a.is_special(value, self):
            self._a.special(value, self, prefix, ctx)
            return
        helper = self._helper(value)
        if helper is not None:
            self._helper_keys(helper, value, prefix, ctx)
            return
        self._unknown(value)

    def _unknown(self, value: ast.expr) -> None:
        self._a.add_unresolved(
            value.lineno, f"**{ast.unparse(value)} from an unknown source"
        )

    def dict_keys(self, name: str, prefix: str, ctx: Ctx) -> None:
        """Keys put into the local dict ``name``."""
        here = (id(self._scope), name)
        if here in ctx.visiting:
            return
        ctx = Ctx(ctx.outcome, ctx.visiting | {here})
        info = self._dicts[name]
        for entry in info.entries:
            condition = condition_for(entry.where, self._a.parents)
            self.value(prefix + entry.key, entry.value, entry.line, condition, ctx)
        for line in info.computed:
            self._a.add_unresolved(line, "computed key name")
        for merge in info.merges:
            self.splat(merge.value, prefix, ctx)

    # -- single values ----------------------------------------------------------------

    def value(
        self,
        path: str,
        value: ast.expr | None,
        line: int,
        condition: str | None,
        ctx: Ctx,
    ) -> None:
        """Add the key ``path`` set to ``value``, plus any nested dict keys."""
        if value is None:
            self._a.add_key(path, line, None, condition, ctx)
        elif isinstance(value, ast.Name) and value.id in self._dicts:
            self._a.add_key(path, line, ReturnType.DICT, condition, ctx)
            self.dict_keys(value.id, f"{path}.", ctx)
        elif _is_dict_literal(value):
            self._a.add_key(path, line, ReturnType.DICT, condition, ctx)
            self._literal(value, f"{path}.", condition, ctx)
        elif self._helper_returning_dict(value):
            self._a.add_key(path, line, ReturnType.DICT, condition, ctx)
            helper = self._helper(value)
            assert helper is not None
            self._helper_keys(helper, value, f"{path}.", ctx)
        elif isinstance(value, ast.Name):
            self._a.add_key(
                path,
                line,
                self._name_type(value.id),
                condition,
                ctx,
                self._name_literal(value.id),
            )
        else:
            self._a.add_key(
                path, line, infer(value).type, condition, ctx, _literal(value)
            )

    def _literal(
        self, value: ast.expr, prefix: str, condition: str | None, ctx: Ctx
    ) -> None:
        """Keys of a dict literal or ``dict(...)`` call."""
        if condition is None:
            condition = condition_for(value, self._a.parents)
        if isinstance(value, ast.Dict):
            for key_node, item in zip(value.keys, value.values, strict=True):
                if key_node is None:
                    self.splat(item, prefix, ctx)
                    continue
                key = _constant_key(key_node)
                if key is None:
                    self._a.add_unresolved(key_node.lineno, "computed key name")
                else:
                    self.value(prefix + key, item, key_node.lineno, condition, ctx)
            return
        assert isinstance(value, ast.Call)
        for arg in value.args:
            self.splat(arg, prefix, ctx)
        self.keyword_args(value.keywords, prefix, condition, ctx)

    def assigned(self, name: str) -> list[ast.expr]:
        """Every value assigned to the plain variable ``name`` in this scope."""
        return list(self._assigned.get(name, []))

    def _name_literal(self, name: str) -> Sample | None:
        """The literal value of a variable assigned exactly once, to a literal."""
        values = self._assigned.get(name, [])
        return _literal(values[0]) if len(values) == 1 else None

    def _name_type(self, name: str) -> ReturnType | None:
        """The type of a plain variable, if every assignment to it agrees."""
        types = {infer(v).type for v in self._assigned.get(name, [])}
        return types.pop() if len(types) == 1 else None

    # -- helpers and wrappers (one level) ---------------------------------------------

    def _helper(self, value: ast.expr) -> ast.AST | None:
        """The function in this file ``value`` calls: ``f(...)`` or ``self.f(...)``."""
        if not isinstance(value, ast.Call):
            return None
        func = value.func
        if isinstance(func, ast.Name):
            return self._a.functions.get(func.id)
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "self"
        ):
            return self._a.functions.get(func.attr)
        return None

    def _helper_returning_dict(self, value: ast.expr) -> bool:
        helper = self._helper(value)
        if helper is None:
            return False
        built = _dict_built_names(helper) | _param_names(helper)
        return any(
            _is_dict_literal(r)
            or (isinstance(r, ast.Name) and r.id in built)
            or self._helper(r) is not None
            for r in returned_values(helper)
        )

    def _helper_keys(
        self, helper: ast.AST, call: ast.expr, prefix: str, ctx: Ctx
    ) -> None:
        name = getattr(helper, "name", "?")
        if self._depth >= _MAX_DEPTH:
            self._a.add_unresolved(
                call.lineno, f"helper {name}() is more than one level deep"
            )
            return
        inner = self._a.tracer(helper, self._depth + 1)
        for returned in returned_values(helper):
            if not isinstance(returned, ast.Name):
                inner.splat(returned, prefix, ctx)
                continue
            if returned.id in inner._dicts:
                inner.dict_keys(returned.id, prefix, ctx)
            # A helper returning one of its parameters passes on the caller's
            # argument, plus whatever it added to it (above).
            argument = _argument_for(helper, call, returned.id)
            if argument is not None:
                self.splat(argument, prefix, ctx)

    def _wrapper_kwargs(self, value: ast.Name, prefix: str, ctx: Ctx) -> None:
        """``**kwargs`` of this function: resolve from each call site of it."""
        if self._depth >= _MAX_DEPTH:
            self._a.add_unresolved(
                value.lineno, f"**{value.id} is forwarded more than one level deep"
            )
            return
        calls = self._a.call_sites(self._scope)
        if not calls:
            self._a.add_unresolved(
                value.lineno, f"**{value.id} has no callers in this file"
            )
            return
        for call in calls:
            caller = self._a.tracer(scope_of(call, self._a.parents), self._depth + 1)
            condition = condition_for(call, self._a.parents)
            caller.keyword_args(call.keywords, prefix, condition, ctx)


# --- helpers ------------------------------------------------------------------------


def parse_file(path: Path) -> ast.Module:
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


def method_calls(tree: ast.AST, method: str) -> Iterator[ast.Call]:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == method
        ):
            yield node


def scope_of(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST:
    """The innermost function containing ``node``, or the module."""
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, _SCOPES):
            return current
    return current


def local_nodes(scope: ast.AST) -> Iterator[ast.AST]:
    """Every node in ``scope`` except those inside nested functions or classes."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _NESTED_SCOPES):
            stack.extend(ast.iter_child_nodes(node))


def returned_values(function: ast.AST) -> list[ast.expr]:
    return [
        node.value
        for node in local_nodes(function)
        if isinstance(node, ast.Return) and node.value is not None
    ]


def _dict_built_names(scope: ast.AST) -> frozenset[str]:
    """Names in ``scope`` assigned a dict literal or given keys like a dict.

    A cheap syntactic check used to decide whether a helper returns a dict,
    without building a full tracer (which could recurse through helpers).
    """
    names: set[str] = set()
    for node in local_nodes(scope):
        if isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and _is_dict_literal(node.value):
                    names.add(target.id)
                if isinstance(target, ast.Subscript) and isinstance(
                    target.value, ast.Name
                ):
                    names.add(target.value.id)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("update", "setdefault")
            and isinstance(node.func.value, ast.Name)
        ):
            names.add(node.func.value.id)
    return frozenset(names)


def _argument_for(function: ast.AST, call: ast.expr, param: str) -> ast.expr | None:
    """The argument ``call`` passes for ``function``'s parameter ``param``."""
    if not (isinstance(call, ast.Call) and isinstance(function, _FUNCTIONS)):
        return None
    for keyword in call.keywords:
        if keyword.arg == param:
            return keyword.value
    positional = [a.arg for a in (*function.args.posonlyargs, *function.args.args)]
    if isinstance(call.func, ast.Attribute) and positional[:1] == ["self"]:
        positional = positional[1:]  # self.method(...): self is bound
    if param in positional:
        index = positional.index(param)
        if index < len(call.args) and not isinstance(call.args[index], ast.Starred):
            return call.args[index]
    return None


def _kwarg_name(scope: ast.AST) -> str | None:
    if isinstance(scope, _FUNCTIONS) and scope.args.kwarg is not None:
        return scope.args.kwarg.arg
    return None


def _param_names(scope: ast.AST) -> frozenset[str]:
    if not isinstance(scope, _SCOPES):
        return frozenset()
    args = scope.args
    params = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    if args.vararg is not None:
        params.append(args.vararg)
    return frozenset(a.arg for a in params)


def _literal(value: ast.expr) -> Sample | None:
    """``value`` as a sample, if it is a plain literal (evaluated without running code).

    ``None`` is not a sample: in code it usually means "not set yet".
    """
    try:
        evaluated = ast.literal_eval(value)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None
    if evaluated is None or not _is_json(evaluated):
        return None
    return Sample(evaluated)


def _is_json(value: object) -> bool:
    if value is None or isinstance(value, bool | int | float | str):
        return True
    if isinstance(value, list | tuple):
        return all(_is_json(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_json(v) for k, v in value.items())
    return False


def _is_dict_literal(value: ast.expr) -> bool:
    return isinstance(value, ast.Dict) or (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "dict"
    )


def _loop_keys(
    node: ast.expr, where: ast.AST, parents: dict[ast.AST, ast.AST], scope: ast.AST
) -> list[str] | None:
    """Keys named by a loop variable over a literal sequence, or ``None``.

    Handles ``for k in ['a', 'b']: d[k]``, ``for p in [('a', x), ...]: d[p[0]]``
    and ``for k, v in [('a', x), ...]: d[k]``. Every item must give a usable
    string key, and the loop body must not rebind the variable; anything less
    certain is left as a computed key (FR-12).
    """
    index: int | None = None
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        if not isinstance(node.slice.value, int) or isinstance(node.slice.value, bool):
            return None
        index, node = node.slice.value, node.value
    if not isinstance(node, ast.Name):
        return None
    loop = _enclosing_loop(node.id, where, parents, scope)
    if loop is None or not isinstance(loop.iter, (ast.List, ast.Tuple, ast.Set)):
        return None
    position = _target_position(loop.target, node.id)
    width = (
        len(loop.target.elts) if isinstance(loop.target, ast.Tuple | ast.List) else 0
    )
    keys: list[str] = []
    for item in loop.iter.elts:
        value: ast.expr | None = item
        if position is not None:
            value = _element(item, position, width)
        if index is not None and value is not None:
            value = _element(value, index)
        key = _constant_key(value) if value is not None else None
        if key is None:
            return None
        if key not in keys:
            keys.append(key)
    return keys


def _enclosing_loop(
    name: str, where: ast.AST, parents: dict[ast.AST, ast.AST], scope: ast.AST
) -> ast.For | None:
    """The innermost ``for`` around ``where`` (within ``scope``) that binds ``name``."""
    node = parents.get(where)
    while node is not None and node is not scope and not isinstance(node, _FUNCTIONS):
        if isinstance(node, ast.For) and _binds(node.target, name):
            return None if _rebound(node, name) else node
        node = parents.get(node)
    return None


def _binds(target: ast.expr, name: str) -> bool:
    if isinstance(target, ast.Name):
        return target.id == name
    if isinstance(target, (ast.Tuple, ast.List)):
        return any(isinstance(e, ast.Name) and e.id == name for e in target.elts)
    return False


def _target_position(target: ast.expr, name: str) -> int | None:
    """Where ``name`` sits in an unpacking target; ``None`` for a plain name."""
    if isinstance(target, (ast.Tuple, ast.List)):
        for position, element in enumerate(target.elts):
            if isinstance(element, ast.Name) and element.id == name:
                return position
    return None


def _rebound(loop: ast.For, name: str) -> bool:
    """Whether the loop body (or else clause) assigns ``name`` again."""
    for statement in [*loop.body, *loop.orelse]:
        for node in ast.walk(statement):
            if (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Store)
                and node.id == name
            ):
                return True
    return False


def _element(value: ast.expr, index: int, length: int | None = None) -> ast.expr | None:
    """Item ``index`` of a literal tuple or list (of exactly ``length`` items)."""
    if not isinstance(value, (ast.Tuple, ast.List)):
        return None
    if length is not None and len(value.elts) != length:
        return None
    if not 0 <= index < len(value.elts):
        return None
    return value.elts[index]


def _constant_key(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value if _usable_key(node.value) else None
    return None


def _usable_key(key: str) -> bool:
    """Keys that can be a dotted path segment: non-empty and without a dot."""
    return bool(key) and "." not in key
