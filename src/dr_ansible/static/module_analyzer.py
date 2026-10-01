"""Find the keys a module returns through ``exit_json`` and ``fail_json``.

Implements FR-8, FR-9 and FR-12. The module is parsed with ``ast`` and never
imported or run. For every ``<anything>.exit_json(...)`` call (success) and
``<anything>.fail_json(...)`` call (failure), keyword arguments are keys, and
a ``**name`` argument is traced back through how ``name`` was built in the
same function:

- dict literals (including ``{**other}``), ``dict(...)``, ``name[key] = ...``,
  ``name.update(...)`` and ``name.setdefault(...)``;
- local helper functions whose ``return`` gives a dict, one level deep;
- for a wrapper such as ``def finish(module, **extra)``, the ``**extra``
  passed at each call site of the wrapper, also one level deep.

A keyword whose value is a dict built that way also yields nested keys
(``info.size``). Tracing is flow-insensitive: every contribution in the
function counts, so a key set on any path is reported rather than missed.

Anything that cannot be named statically (a computed key, ``**`` from a
parameter, an attribute or an unknown call, a helper or wrapper deeper than
one level) is reported as :class:`~dr_ansible.model.Unresolved` with its
location, never guessed.
"""

import ast
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from dr_ansible.model import Outcome, ReturnType, StaticKey, Unresolved
from dr_ansible.static.infer import condition_for, infer, parent_map

_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_NESTED_SCOPES = (*_SCOPES, ast.ClassDef)
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_RESULT_METHODS = {"exit_json": Outcome.SUCCESS, "fail_json": Outcome.FAILURE}
#: How many helper or wrapper hops are followed (FR-9: "one level deep").
_MAX_DEPTH = 1


class AnalysisError(Exception):
    """The file cannot be read or is not valid Python (NFR-8)."""


@dataclass(frozen=True, slots=True)
class ModuleAnalysis:
    """Static evidence from one module file, sorted for deterministic output."""

    keys: tuple[StaticKey, ...] = ()
    unresolved: tuple[Unresolved, ...] = ()


def analyze_module(path: Path) -> ModuleAnalysis:
    """Collect the keys ``path`` passes to ``exit_json`` and ``fail_json``."""
    tree = _parse(path)
    analysis = _Analysis(path, tree)
    for method, outcome in _RESULT_METHODS.items():
        for call in _method_calls(tree, method):
            tracer = analysis.tracer(_scope_of(call, analysis.parents), depth=0)
            tracer.result_call(call, outcome)
    return analysis.result()


# --- shared state -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Ctx:
    """What a key being traced inherits: its outcome and the dicts on the path."""

    outcome: Outcome
    visiting: frozenset[str] = frozenset()


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


class _Analysis:
    def __init__(self, path: Path, tree: ast.Module):
        self.path = path
        self.parents = parent_map(tree)
        self.functions = {
            node.name: node for node in ast.walk(tree) if isinstance(node, _FUNCTIONS)
        }
        self._tree = tree
        self._tracers: dict[tuple[ast.AST, int], _Tracer] = {}
        self._keys: set[StaticKey] = set()
        self._unresolved: set[Unresolved] = set()

    def tracer(self, scope: ast.AST, depth: int) -> "_Tracer":
        key = (scope, depth)
        if key not in self._tracers:
            self._tracers[key] = _Tracer(self, scope, depth)
        return self._tracers[key]

    def add_key(
        self,
        path: str,
        line: int,
        type_: ReturnType | None,
        condition: str | None,
        ctx: _Ctx,
    ) -> None:
        self._keys.add(
            StaticKey(
                path=path,
                file=self.path,
                line=line,
                outcome=ctx.outcome,
                inferred_type=type_,
                condition=condition,
            )
        )

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

    def result(self) -> ModuleAnalysis:
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
        return ModuleAnalysis(keys=tuple(keys), unresolved=tuple(unresolved))


# --- tracing within one scope -------------------------------------------------------


class _Tracer:
    """Traces names within one function (or the module body).

    ``depth`` counts helper or wrapper hops already taken; at
    :data:`_MAX_DEPTH` no further hop is followed.
    """

    def __init__(self, analysis: _Analysis, scope: ast.AST, depth: int):
        self._a = analysis
        self._scope = scope
        self._depth = depth
        self._dicts: dict[str, _DictInfo] = {}
        self._assigned: dict[str, list[ast.expr]] = {}
        self._kwarg = _kwarg_name(scope)
        self._params = _param_names(scope)
        for node in _local_nodes(scope):
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
        if _is_dict_literal(value) or self._helper_returning_dict(value):
            self._info(target.id).merges.append(_Merge(value, stmt))

    def _index_subscript(
        self, target: ast.expr, value: ast.expr, stmt: ast.AST
    ) -> None:
        if not (
            isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
        ):
            return
        info = self._info(target.value.id)
        key = _constant_key(target.slice)
        if key is None:
            info.computed.append(target.lineno)
        else:
            info.entries.append(_Entry(key, value, target.lineno, stmt))

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
            key = _constant_key(call.args[0])
            if key is None:
                info.computed.append(call.lineno)
            else:
                default = call.args[1] if len(call.args) > 1 else None
                info.entries.append(_Entry(key, default, call.lineno, call))

    # -- the entry point: an exit_json / fail_json call -------------------------------

    def result_call(self, call: ast.Call, outcome: Outcome) -> None:
        ctx = _Ctx(outcome)
        condition = condition_for(call, self._a.parents)
        self.keyword_args(call.keywords, "", condition, ctx)

    def keyword_args(
        self,
        keywords: list[ast.keyword],
        prefix: str,
        condition: str | None,
        ctx: _Ctx,
    ) -> None:
        for keyword in keywords:
            if keyword.arg is None:
                self.splat(keyword.value, prefix, ctx)
            elif _usable_key(keyword.arg):
                self.value(
                    prefix + keyword.arg, keyword.value, keyword.lineno, condition, ctx
                )

    # -- splats: **value --------------------------------------------------------------

    def splat(self, value: ast.expr, prefix: str, ctx: _Ctx) -> None:
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
        helper = self._helper(value)
        if helper is not None:
            self._helper_keys(helper, value, prefix, ctx)
            return
        self._unknown(value)

    def _unknown(self, value: ast.expr) -> None:
        self._a.add_unresolved(
            value.lineno, f"**{ast.unparse(value)} from an unknown source"
        )

    def dict_keys(self, name: str, prefix: str, ctx: _Ctx) -> None:
        """Keys put into the local dict ``name``."""
        if name in ctx.visiting:
            return
        ctx = _Ctx(ctx.outcome, ctx.visiting | {name})
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
        ctx: _Ctx,
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
            self._a.add_key(path, line, self._name_type(value.id), condition, ctx)
        else:
            self._a.add_key(path, line, infer(value).type, condition, ctx)

    def _literal(
        self, value: ast.expr, prefix: str, condition: str | None, ctx: _Ctx
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

    def _name_type(self, name: str) -> ReturnType | None:
        """The type of a plain variable, if every assignment to it agrees."""
        types = {infer(v).type for v in self._assigned.get(name, [])}
        return types.pop() if len(types) == 1 else None

    # -- helpers and wrappers (one level) ---------------------------------------------

    def _helper(self, value: ast.expr) -> ast.AST | None:
        """The local function ``value`` calls, if it is a call to one."""
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
            return self._a.functions.get(value.func.id)
        return None

    def _helper_returning_dict(self, value: ast.expr) -> bool:
        helper = self._helper(value)
        if helper is None:
            return False
        built = _dict_built_names(helper)
        return any(
            _is_dict_literal(r)
            or (isinstance(r, ast.Name) and r.id in built)
            or self._helper(r) is not None
            for r in _returned_values(helper)
        )

    def _helper_keys(
        self, helper: ast.AST, call: ast.expr, prefix: str, ctx: _Ctx
    ) -> None:
        name = getattr(helper, "name", "?")
        if self._depth >= _MAX_DEPTH:
            self._a.add_unresolved(
                call.lineno, f"helper {name}() is more than one level deep"
            )
            return
        inner = self._a.tracer(helper, self._depth + 1)
        for returned in _returned_values(helper):
            if isinstance(returned, ast.Name) and returned.id not in inner._dicts:
                continue  # a returned non-dict: nothing to add
            inner.splat(returned, prefix, ctx)

    def _wrapper_kwargs(self, value: ast.Name, prefix: str, ctx: _Ctx) -> None:
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
            caller = self._a.tracer(_scope_of(call, self._a.parents), self._depth + 1)
            condition = condition_for(call, self._a.parents)
            caller.keyword_args(call.keywords, prefix, condition, ctx)


# --- helpers ------------------------------------------------------------------------


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


def _returned_values(function: ast.AST) -> list[ast.expr]:
    return [
        node.value
        for node in _local_nodes(function)
        if isinstance(node, ast.Return) and node.value is not None
    ]


def _dict_built_names(scope: ast.AST) -> frozenset[str]:
    """Names in ``scope`` assigned a dict literal or given keys like a dict.

    A cheap syntactic check used to decide whether a helper returns a dict,
    without building a full tracer (which could recurse through helpers).
    """
    names: set[str] = set()
    for node in _local_nodes(scope):
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


def _is_dict_literal(value: ast.expr) -> bool:
    return isinstance(value, ast.Dict) or (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "dict"
    )


def _constant_key(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value if _usable_key(node.value) else None
    return None


def _usable_key(key: str) -> bool:
    """Keys that can be a dotted path segment: non-empty and without a dot."""
    return bool(key) and "." not in key
