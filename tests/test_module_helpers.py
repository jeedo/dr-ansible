"""Tests for helpers, wrappers, fail_json and unresolved keys (plan task 14).

Covers FR-9 (one-level helpers, failure-only keys) and FR-12 (dynamic keys are
reported as unresolved with their location, never guessed).
"""

from pathlib import Path

from dr_ansible.model import Outcome, ReturnType, Unresolved
from dr_ansible.static.module_analyzer import ModuleAnalysis, analyze_module

MODULES = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "core"
    / "lib"
    / "ansible"
    / "modules"
)
T = ReturnType


def _line(path: Path, snippet: str) -> int:
    for number, text in enumerate(path.read_text().splitlines(), start=1):
        if snippet in text:
            return number
    raise AssertionError(f"{snippet!r} not in {path}")


def _analyze(tmp_path: Path, source: str) -> ModuleAnalysis:
    path = tmp_path / "thing.py"
    path.write_text(source)
    return analyze_module(path)


def _paths(analysis: ModuleAnalysis, outcome: Outcome | None = None) -> list[str]:
    return [k.path for k in analysis.keys if outcome is None or k.outcome is outcome]


def _reasons(analysis: ModuleAnalysis) -> list[tuple[int, str]]:
    return [(u.line, u.reason) for u in analysis.unresolved]


# --- fixtures -----------------------------------------------------------------------


def test_helper_fixture_keys_come_from_the_helper_return() -> None:
    path = MODULES / "helper.py"
    analysis = analyze_module(path)
    by_path = {k.path: k for k in analysis.keys}
    assert sorted(by_path) == ["changed", "name", "owner", "state"]
    helper_line = _line(path, 'return {"name": name')
    assert {by_path[k].line for k in ("name", "owner", "state")} == {helper_line}
    assert by_path["state"].inferred_type is T.STR
    assert by_path["name"].inferred_type is None  # a parameter of the helper
    assert analysis.unresolved == ()


def test_dynamic_fixture_resolves_the_wrapper_and_reports_the_computed_key() -> None:
    path = MODULES / "dynamic.py"
    analysis = analyze_module(path)
    by_path = {k.path: k for k in analysis.keys}
    assert sorted(by_path) == ["changed", "status"]
    assert by_path["status"].line == _line(path, 'result = {"status": "ok"}')
    assert analysis.unresolved == (
        Unresolved(
            file=path,
            line=_line(path, 'result[f"{prefix}_id"] = 1001'),
            reason="computed key name",
        ),
    )


def test_keywords_fixture_has_failure_only_keys() -> None:
    analysis = analyze_module(MODULES / "keywords.py")
    assert _paths(analysis, Outcome.FAILURE) == ["msg", "rc", "stderr"]
    assert "rc" not in _paths(analysis, Outcome.SUCCESS)
    failure = {k.path: k for k in analysis.keys if k.outcome is Outcome.FAILURE}
    assert failure["rc"].inferred_type is T.INT
    assert failure["rc"].condition == "module.params['fail']"


# --- fail_json ----------------------------------------------------------------------


def test_fail_json_is_traced_like_exit_json(tmp_path: Path) -> None:
    analysis = _analyze(
        tmp_path,
        "def main():\n"
        "    r = {'stdout': ''}\n"
        "    if rc:\n"
        "        m.fail_json(msg='bad', **r)\n"
        "    m.exit_json(**r)\n",
    )
    assert [(k.path, k.outcome, k.condition) for k in analysis.keys] == [
        ("msg", Outcome.FAILURE, "rc"),
        ("stdout", Outcome.FAILURE, None),
        ("stdout", Outcome.SUCCESS, None),
    ]


# --- helpers (one level) ------------------------------------------------------------


def test_splatted_helper_call(tmp_path: Path) -> None:
    analysis = _analyze(
        tmp_path,
        "def info():\n"
        "    return dict(version='1', ok=True)\n"
        "def main():\n"
        "    m.exit_json(**info())\n",
    )
    assert [(k.path, k.inferred_type, k.line) for k in analysis.keys] == [
        ("ok", T.BOOL, 2),
        ("version", T.STR, 2),
    ]


def test_helper_building_a_dict_inside(tmp_path: Path) -> None:
    analysis = _analyze(
        tmp_path,
        "def build():\n"
        "    out = {}\n"
        "    if flag:\n"
        "        out['x'] = 1\n"
        "    return out\n"
        "def main():\n"
        "    r = build()\n"
        "    r['y'] = 'two'\n"
        "    m.exit_json(**r)\n",
    )
    assert [(k.path, k.line, k.condition) for k in analysis.keys] == [
        ("x", 4, "flag"),
        ("y", 8, None),
    ]


def test_helper_result_as_a_keyword_value_gives_nested_keys(tmp_path: Path) -> None:
    analysis = _analyze(
        tmp_path,
        "def stat():\n    return {'size': 1}\n"
        "def main():\n    m.exit_json(info=stat())\n",
    )
    assert [(k.path, k.inferred_type) for k in analysis.keys] == [
        ("info", T.DICT),
        ("info.size", T.INT),
    ]


def test_helper_returning_a_non_dict_is_not_a_dict(tmp_path: Path) -> None:
    analysis = _analyze(
        tmp_path,
        "def name():\n    return 'x'\ndef main():\n    m.exit_json(n=name())\n",
    )
    assert [(k.path, k.inferred_type) for k in analysis.keys] == [("n", None)]
    assert analysis.unresolved == ()


def test_helpers_are_followed_one_level_only(tmp_path: Path) -> None:
    analysis = _analyze(
        tmp_path,
        "def inner():\n"
        "    return {'deep': 1}\n"
        "def outer():\n"
        "    return inner()\n"
        "def main():\n"
        "    m.exit_json(**outer())\n",
    )
    assert analysis.keys == ()
    assert _reasons(analysis) == [(4, "helper inner() is more than one level deep")]


def test_recursive_helper_does_not_loop(tmp_path: Path) -> None:
    analysis = _analyze(
        tmp_path,
        "def build():\n"
        "    r = {'a': 1}\n"
        "    r.update(build())\n"
        "    return r\n"
        "def main():\n"
        "    m.exit_json(**build())\n",
    )
    assert _paths(analysis) == ["a"]


# --- wrappers -----------------------------------------------------------------------


def test_wrapper_kwargs_are_resolved_from_every_call_site(tmp_path: Path) -> None:
    analysis = _analyze(
        tmp_path,
        "def done(module, **kw):\n"
        "    module.exit_json(changed=True, **kw)\n"
        "def main():\n"
        "    if a:\n"
        "        done(m, first=1)\n"
        "    r = {'second': 'x'}\n"
        "    done(m, **r)\n",
    )
    assert [(k.path, k.line, k.condition) for k in analysis.keys] == [
        ("changed", 2, None),
        ("first", 5, "a"),
        ("second", 6, None),
    ]


def test_wrapper_without_callers_is_unresolved(tmp_path: Path) -> None:
    analysis = _analyze(
        tmp_path, "def done(module, **kw):\n    module.exit_json(**kw)\n"
    )
    assert _reasons(analysis) == [(2, "**kw has no callers in this file")]


def test_wrapper_chains_stop_after_one_level(tmp_path: Path) -> None:
    analysis = _analyze(
        tmp_path,
        "def done(module, **kw):\n"
        "    module.exit_json(**kw)\n"
        "def relay(module, **kw):\n"
        "    done(module, **kw)\n"
        "def main():\n"
        "    relay(m, x=1)\n",
    )
    assert analysis.keys == ()
    assert _reasons(analysis) == [(4, "**kw is forwarded more than one level deep")]


# --- merges -------------------------------------------------------------------------


def test_update_and_unpack_merge_local_dicts(tmp_path: Path) -> None:
    analysis = _analyze(
        tmp_path,
        "def main():\n"
        "    base = {'a': 1}\n"
        "    extra = dict(b=2)\n"
        "    r = {**base, 'c': 3}\n"
        "    r.update(extra)\n"
        "    m.exit_json(**r)\n",
    )
    assert [(k.path, k.line) for k in analysis.keys] == [("a", 2), ("b", 3), ("c", 4)]
    assert analysis.unresolved == ()


# --- unresolved (FR-12) -------------------------------------------------------------


def test_each_kind_of_unresolved(tmp_path: Path) -> None:
    analysis = _analyze(
        tmp_path,
        "def main(extra):\n"
        "    r = {'ok': 1, key: 2, **unknown}\n"
        "    r[name] = 3\n"
        "    r.setdefault(other_name, 4)\n"
        "    r.update(other)\n"
        "    r.update(compute())\n"
        "    m.exit_json(**r, **extra, **m.params, **getattr(x, 'y'))\n",
    )
    assert _paths(analysis) == ["ok"]
    assert _reasons(analysis) == [
        (2, "**unknown from an unknown source"),
        (2, "computed key name"),
        (3, "computed key name"),
        (4, "computed key name"),
        (5, "**other from an unknown source"),
        (6, "**compute() from an unknown source"),
        (7, "**extra from a parameter"),
        (7, "**getattr(x, 'y') from an unknown source"),
        (7, "**m.params from an unknown source"),
    ]
    assert all(u.file == tmp_path / "thing.py" for u in analysis.unresolved)


def test_unresolved_inside_nested_keyword_values(tmp_path: Path) -> None:
    analysis = _analyze(tmp_path, "def main():\n    m.exit_json(info={k: 1, 'a': 2})\n")
    assert _paths(analysis) == ["info", "info.a"]
    assert _reasons(analysis) == [(2, "computed key name")]


def test_unresolved_are_deduplicated(tmp_path: Path) -> None:
    analysis = _analyze(
        tmp_path,
        "def main():\n"
        "    r = {}\n"
        "    r[k] = 1\n"
        "    m.exit_json(**r)\n"
        "    m.fail_json(msg='x', **r)\n",
    )
    assert _reasons(analysis) == [(3, "computed key name")]


def test_helper_returning_a_non_dict_variable_is_not_a_dict(tmp_path: Path) -> None:
    analysis = _analyze(
        tmp_path,
        "def label():\n"
        "    text = 'x'\n"
        "    return text\n"
        "def main():\n"
        "    m.exit_json(n=label())\n",
    )
    assert [(k.path, k.inferred_type) for k in analysis.keys] == [("n", None)]


def test_kwargs_extended_locally_still_resolve_callers(tmp_path: Path) -> None:
    analysis = _analyze(
        tmp_path,
        "def done(module, **kw):\n"
        "    kw['local'] = 1\n"
        "    module.exit_json(**kw)\n"
        "def main():\n"
        "    done(m, from_caller='x')\n",
    )
    assert _paths(analysis) == ["from_caller", "local"]


def test_parameter_extended_locally_is_still_unresolved(tmp_path: Path) -> None:
    analysis = _analyze(
        tmp_path,
        "def done(module, result):\n"
        "    result['local'] = 1\n"
        "    module.exit_json(**result)\n",
    )
    assert _paths(analysis) == ["local"]
    assert _reasons(analysis) == [(3, "**result from a parameter")]
