"""Find the keys an action plugin returns from ``ActionModule.run()`` (FR-10).

Every ``return`` in ``run()`` is traced with the shared tracer (see
:mod:`~dr_ansible.static.tracer`), so ``result[key] = ...``,
``result.update(...)``, ``return dict(...)``, local helper functions and
unresolved keys work as they do for modules. On top of that, these Ansible
calls are understood:

- ``super().run(...)``: the base ``ActionBase`` result, ignored silently;
- ``self._execute_module(...)``: the module's own returns are merged, so the
  plugin inherits from the module named by ``module_name`` (keyword or first
  argument), or from its own module when none is given (AC-7);
- ``<...>.action_loader.get('<name>', ...)`` then ``<that>.run(...)``:
  delegation to another action plugin, as ``template`` does to ``copy``, so
  the plugin inherits from ``<name>``;
- ``merge_hash(a, b)`` and ``combine_vars(a, b)``: both sides are merged.

All keys are recorded with ``outcome=success``: ``run()`` gives no reliable
signal for telling success paths from failure paths. The plugin is parsed with
``ast`` and never imported or run.
"""

import ast
from dataclasses import dataclass
from pathlib import Path

from dr_ansible.model import Outcome, StaticKey, Unresolved
from dr_ansible.static.tracer import (
    Analysis,
    Ctx,
    Tracer,
    parse_file,
    returned_values,
)

_MERGE_FUNCTIONS = frozenset({"merge_hash", "combine_vars"})
_RUN_METHODS = (ast.FunctionDef, ast.AsyncFunctionDef)


@dataclass(frozen=True, slots=True)
class ActionAnalysis:
    """Static evidence from one action plugin.

    ``inherits_from`` names the modules (or delegated action plugins) whose
    returns the plugin passes on, sorted.
    """

    keys: tuple[StaticKey, ...] = ()
    unresolved: tuple[Unresolved, ...] = ()
    inherits_from: tuple[str, ...] = ()


def analyze_action(path: Path, own_module: str) -> ActionAnalysis:
    """Collect what ``ActionModule.run()`` in ``path`` returns.

    ``own_module`` is the FQCN of the module this plugin belongs to, used when
    ``_execute_module()`` is called without a module name.
    """
    tree = parse_file(path)
    run = _run_method(tree)
    if run is None:
        return ActionAnalysis()

    analysis = _ActionAnalysis(path, tree, own_module)
    tracer = analysis.tracer(run, depth=0)
    ctx = Ctx(Outcome.SUCCESS)
    for value in returned_values(run):
        tracer.splat(value, "", ctx)
    keys, unresolved = analysis.result()
    return ActionAnalysis(
        keys=keys,
        unresolved=unresolved,
        inherits_from=tuple(sorted(analysis.inherits)),
    )


class _ActionAnalysis(Analysis):
    def __init__(self, path: Path, tree: ast.Module, own_module: str):
        super().__init__(path, tree)
        self.own_module = own_module
        self.inherits: set[str] = set()

    def is_special(self, value: ast.expr, tracer: Tracer) -> bool:
        return isinstance(value, ast.Call) and (
            _is_super_run(value)
            or _is_method(value, "_execute_module")
            or _is_merge(value)
            or _delegate(value, tracer) is not None
        )

    def special(self, value: ast.expr, tracer: Tracer, prefix: str, ctx: Ctx) -> None:
        assert isinstance(value, ast.Call)
        if _is_super_run(value):
            return  # the base ActionBase result
        if _is_method(value, "_execute_module"):
            self._execute_module(value)
        elif _is_merge(value):
            for arg in value.args:
                tracer.splat(arg, prefix, ctx)
        else:
            delegate = _delegate(value, tracer)
            assert delegate is not None
            self.inherits.add(delegate)

    def _execute_module(self, call: ast.Call) -> None:
        name_node = next(
            (k.value for k in call.keywords if k.arg == "module_name"),
            call.args[0] if call.args else None,
        )
        if name_node is None:
            self.inherits.add(self.own_module)
        elif isinstance(name_node, ast.Constant) and isinstance(name_node.value, str):
            self.inherits.add(name_node.value)
        else:
            self.add_unresolved(
                call.lineno, "module name of _execute_module() cannot be determined"
            )


def _run_method(tree: ast.Module) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "ActionModule":
            for item in node.body:
                if isinstance(item, _RUN_METHODS) and item.name == "run":
                    return item
    return None


def _is_method(call: ast.Call, name: str) -> bool:
    return isinstance(call.func, ast.Attribute) and call.func.attr == name


def _is_super_run(call: ast.Call) -> bool:
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "run"
        and isinstance(func.value, ast.Call)
        and isinstance(func.value.func, ast.Name)
        and func.value.func.id == "super"
    )


def _is_merge(call: ast.Call) -> bool:
    func = call.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    return name in _MERGE_FUNCTIONS


def _delegate(call: ast.Call, tracer: Tracer) -> str | None:
    """The action plugin ``call`` runs, for ``x.run(...)`` with ``x`` from a loader."""
    func = call.func
    if not (
        isinstance(func, ast.Attribute)
        and func.attr == "run"
        and isinstance(func.value, ast.Name)
    ):
        return None
    for value in tracer.assigned(func.value.id):
        if (
            isinstance(value, ast.Call)
            and _dotted(value.func).endswith("action_loader.get")
            and value.args
            and isinstance(value.args[0], ast.Constant)
            and isinstance(value.args[0].value, str)
        ):
            return value.args[0].value
    return None


def _dotted(node: ast.expr) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))
