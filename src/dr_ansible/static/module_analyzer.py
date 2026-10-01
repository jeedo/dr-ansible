"""Find the keys a module returns through ``exit_json`` and ``fail_json``.

Implements FR-8, FR-9 and FR-12. For every ``<anything>.exit_json(...)`` call
(success) and ``<anything>.fail_json(...)`` call (failure), keyword arguments
are keys, and a ``**name`` argument is traced back through how ``name`` was
built (see :mod:`~dr_ansible.static.tracer`): dict literals, ``dict(...)``,
``name[key] = ...``, ``name.update(...)``, ``name.setdefault(...)``, local
helper functions and a wrapper's call sites, one level deep each. What cannot
be named statically is reported as unresolved, never guessed. The module is
parsed with ``ast`` and never imported or run.
"""

from dataclasses import dataclass
from pathlib import Path

from dr_ansible.model import Outcome, StaticKey, Unresolved
from dr_ansible.static.tracer import (
    Analysis,
    AnalysisError,
    method_calls,
    parse_file,
    scope_of,
)

__all__ = ["AnalysisError", "ModuleAnalysis", "analyze_module"]

_RESULT_METHODS = {"exit_json": Outcome.SUCCESS, "fail_json": Outcome.FAILURE}


@dataclass(frozen=True, slots=True)
class ModuleAnalysis:
    """Static evidence from one module file, sorted for deterministic output."""

    keys: tuple[StaticKey, ...] = ()
    unresolved: tuple[Unresolved, ...] = ()


def analyze_module(path: Path) -> ModuleAnalysis:
    """Collect the keys ``path`` passes to ``exit_json`` and ``fail_json``."""
    tree = parse_file(path)
    analysis = Analysis(path, tree)
    for method, outcome in _RESULT_METHODS.items():
        for call in method_calls(tree, method):
            tracer = analysis.tracer(scope_of(call, analysis.parents), depth=0)
            tracer.result_call(call, outcome)
    keys, unresolved = analysis.result()
    return ModuleAnalysis(keys=keys, unresolved=unresolved)
