"""Tests for action plugin analysis (plan task 15: FR-10, AC-7)."""

import sys
from pathlib import Path

import pytest

from dr_ansible.model import Outcome, ReturnType
from dr_ansible.static.action_analyzer import ActionAnalysis, analyze_action
from dr_ansible.static.tracer import AnalysisError

ACTIONS = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "core"
    / "lib"
    / "ansible"
    / "plugins"
    / "action"
)
T = ReturnType


def _line(path: Path, snippet: str) -> int:
    for number, text in enumerate(path.read_text().splitlines(), start=1):
        if snippet in text:
            return number
    raise AssertionError(f"{snippet!r} not in {path}")


def _plugin(tmp_path: Path, run_body: str, *, extra: str = "") -> ActionAnalysis:
    path = tmp_path / "thing.py"
    body = "".join(f"        {line}\n" for line in run_body.splitlines())
    path.write_text(
        f"{extra}"
        "class ActionModule(ActionBase):\n"
        "    def run(self, tmp=None, task_vars=None):\n"
        f"{body}"
    )
    return analyze_action(path, "ns.coll.thing")


def _paths(analysis: ActionAnalysis) -> list[str]:
    return [k.path for k in analysis.keys]


# --- fixtures -----------------------------------------------------------------------


def test_virtual_action_builds_the_whole_result() -> None:
    path = ACTIONS / "virtual.py"
    analysis = analyze_action(path, "ansible.builtin.virtual")
    keys = {k.path: k for k in analysis.keys}
    assert sorted(keys) == [
        "changed",
        "checksum",
        "dest",
        "failed",
        "file",
        "msg",
        "remote_checksum",
        "src",
    ]
    assert keys["dest"].line == _line(path, "result.update(changed=True")
    assert keys["dest"].inferred_type is T.STR
    assert keys["checksum"].line == _line(path, 'result["checksum"] = "6e64')
    assert keys["remote_checksum"].condition == "task_vars.get('validate')"
    assert keys["msg"].condition == "src is None"
    assert keys["file"].line == _line(path, "return dict(result, file=src)")
    assert all(k.outcome is Outcome.SUCCESS for k in analysis.keys)
    assert all(k.file == path for k in analysis.keys)
    assert analysis.inherits_from == ()
    assert analysis.unresolved == ()


def test_hybrid_action_inherits_the_module_returns() -> None:
    analysis = analyze_action(ACTIONS / "hybrid.py", "ansible.builtin.hybrid")
    assert analysis.inherits_from == ("ansible.builtin.hybrid",)
    assert _paths(analysis) == ["transferred"]
    assert analysis.unresolved == ()


def test_import_trap_style_safety() -> None:
    before = set(sys.modules)
    analyze_action(ACTIONS / "virtual.py", "ansible.builtin.virtual")
    assert not {m for m in set(sys.modules) - before if "fixtures" in m}


# --- _execute_module ----------------------------------------------------------------


def test_execute_module_without_a_name_inherits_the_own_module(tmp_path: Path) -> None:
    analysis = _plugin(
        tmp_path,
        "result = super().run(tmp, task_vars)\n"
        "result.update(self._execute_module(task_vars=task_vars))\n"
        "return result",
    )
    assert analysis.inherits_from == ("ns.coll.thing",)


def test_execute_module_name_as_first_positional_argument(tmp_path: Path) -> None:
    analysis = _plugin(tmp_path, "return self._execute_module('ansible.legacy.stat')")
    assert analysis.inherits_from == ("ansible.legacy.stat",)


def test_execute_module_assigned_then_extended(tmp_path: Path) -> None:
    analysis = _plugin(
        tmp_path,
        "result = self._execute_module(module_name='ns.coll.other')\n"
        "result['extra'] = 1\n"
        "return result",
    )
    assert analysis.inherits_from == ("ns.coll.other",)
    assert _paths(analysis) == ["extra"]


def test_execute_module_with_a_computed_name_is_unresolved(tmp_path: Path) -> None:
    analysis = _plugin(
        tmp_path, "return self._execute_module(module_name=name, task_vars=task_vars)"
    )
    assert analysis.inherits_from == ()
    assert [(u.line, u.reason) for u in analysis.unresolved] == [
        (3, "module name of _execute_module() cannot be determined")
    ]


def test_merge_hash_merges_both_sides(tmp_path: Path) -> None:
    analysis = _plugin(
        tmp_path,
        "result = super().run(tmp, task_vars)\n"
        "result['own'] = True\n"
        "module_return = self._execute_module(module_name='ansible.legacy.copy')\n"
        "result = merge_hash(result, module_return)\n"
        "return result",
    )
    assert analysis.inherits_from == ("ansible.legacy.copy",)
    assert _paths(analysis) == ["own"]
    assert analysis.unresolved == ()


# --- delegation to another action plugin --------------------------------------------


def test_delegation_to_another_action_plugin(tmp_path: Path) -> None:
    analysis = _plugin(
        tmp_path,
        "result = super().run(tmp, task_vars)\n"
        "copy_action = self._shared_loader_obj.action_loader.get(\n"
        "    'ansible.legacy.copy', task=new_task, connection=self._connection,\n"
        ")\n"
        "result.update(copy_action.run(task_vars=task_vars))\n"
        "return result",
    )
    assert analysis.inherits_from == ("ansible.legacy.copy",)
    assert analysis.unresolved == ()


# --- what run() returns -------------------------------------------------------------


def test_super_run_is_the_silent_base_result(tmp_path: Path) -> None:
    analysis = _plugin(
        tmp_path,
        "result = super(ActionModule, self).run(tmp, task_vars)\n"
        "result['x'] = 1\n"
        "return result",
    )
    assert _paths(analysis) == ["x"]
    assert analysis.unresolved == ()


def test_return_dict_and_literals(tmp_path: Path) -> None:
    analysis = _plugin(
        tmp_path,
        "if bad:\n"
        "    return {'failed': True, 'msg': 'no'}\n"
        "return dict(changed=False, items=[1, 2])",
    )
    assert [(k.path, k.inferred_type, k.condition) for k in analysis.keys] == [
        ("changed", T.BOOL, None),
        ("failed", T.BOOL, "bad"),
        ("items", T.LIST, None),
        ("msg", T.STR, "bad"),
    ]


def test_helper_methods_are_unresolved_not_guessed(tmp_path: Path) -> None:
    analysis = _plugin(tmp_path, "return self._build(task_vars)")
    assert [(u.line, u.reason) for u in analysis.unresolved] == [
        (3, "**self._build(task_vars) from an unknown source")
    ]


def test_module_level_helper_functions_are_followed(tmp_path: Path) -> None:
    analysis = _plugin(
        tmp_path,
        "return build()",
        extra="def build():\n    return {'built': 1}\n",
    )
    assert _paths(analysis) == ["built"]


def test_nested_functions_inside_run_are_not_returns_of_run(tmp_path: Path) -> None:
    analysis = _plugin(
        tmp_path,
        "def inner():\n    return {'not_returned': 1}\nreturn {'returned': 1}",
    )
    assert _paths(analysis) == ["returned"]


def test_no_action_module_class_gives_an_empty_analysis(tmp_path: Path) -> None:
    path = tmp_path / "thing.py"
    path.write_text("def run():\n    return {'a': 1}\n")
    assert analyze_action(path, "ns.coll.thing") == ActionAnalysis()


def test_async_run_is_analysed(tmp_path: Path) -> None:
    path = tmp_path / "thing.py"
    path.write_text(
        "class ActionModule(ActionBase):\n"
        "    async def run(self, tmp=None, task_vars=None):\n"
        "        return {'a': 1}\n"
    )
    assert _paths(analyze_action(path, "ns.coll.thing")) == ["a"]


def test_syntax_error_raises(tmp_path: Path) -> None:
    path = tmp_path / "broken.py"
    path.write_text("class ActionModule(:\n")
    with pytest.raises(AnalysisError, match="SyntaxError"):
        analyze_action(path, "ns.coll.broken")


# --- methods of ActionModule, as in the real copy action ----------------------------

COPY_LIKE = """\
class ActionModule(ActionBase):
    def _ensure_invocation(self, result, task_vars=None):
        if "invocation" not in result:
            result["invocation"] = self._task.args.copy()
        return result

    def run(self, tmp=None, task_vars=None):
        try:
            result = self._run(task_vars=task_vars)
            return self._ensure_invocation(result, task_vars=task_vars)
        except AnsibleActionFail as aaf:
            raise

    def _run(self, tmp=None, task_vars=None):
        result = super(ActionModule, self).run(tmp, task_vars)
        if remote_src:
            result.update(self._execute_module(module_name="ansible.legacy.copy"))
            return result
        module_return = self._copy_file(source, task_vars)
        result.update(module_return)
        result["dest"] = dest
        return result

    def _copy_file(self, source, task_vars):
        result = {}
        result["size"] = 1
        return result
"""


def test_self_methods_are_followed_one_level(tmp_path: Path) -> None:
    path = tmp_path / "copy.py"
    path.write_text(COPY_LIKE)
    analysis = analyze_action(path, "ansible.builtin.copy")
    assert analysis.inherits_from == ("ansible.legacy.copy",)
    assert _paths(analysis) == ["dest", "invocation"]
    assert [u.reason for u in analysis.unresolved] == [
        "helper _copy_file() is more than one level deep"
    ]


def test_helper_returning_its_parameter_passes_the_callers_argument(
    tmp_path: Path,
) -> None:
    analysis = _plugin(
        tmp_path,
        "result = {'from_run': 1}\nreturn tag(result)",
        extra="def tag(r):\n    r['tagged'] = True\n    return r\n",
    )
    assert _paths(analysis) == ["from_run", "tagged"]
    assert analysis.unresolved == ()


def test_returned_parameter_matched_by_keyword(tmp_path: Path) -> None:
    analysis = _plugin(
        tmp_path,
        "return tag(extra=1, r={'a': 1})",
        extra="def tag(r, extra=None):\n    return r\n",
    )
    assert _paths(analysis) == ["a"]
