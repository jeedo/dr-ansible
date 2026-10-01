"""Tests for exit_json key extraction and **name tracing (plan task 13: FR-8, FR-9).

Line numbers are looked up from the fixture text, so the expectations do not
silently depend on the fixtures' exact layout.
"""

import re
import sys
from pathlib import Path

import pytest

from dr_ansible.model import Outcome, ReturnType, StaticKey
from dr_ansible.static.module_analyzer import AnalysisError, analyze_module

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


def _key(
    path: Path,
    key: str,
    snippet: str,
    type_: ReturnType | None,
    condition: str | None = None,
) -> StaticKey:
    return StaticKey(
        path=key,
        file=path,
        line=_line(path, snippet),
        outcome=Outcome.SUCCESS,
        inferred_type=type_,
        condition=condition,
    )


def _analyze_source(tmp_path: Path, source: str) -> tuple[Path, list[StaticKey]]:
    path = tmp_path / "thing.py"
    path.write_text(source)
    return path, list(analyze_module(path).keys)


# --- fixtures -------------------------------------------------------------------------


def test_incremental_result_is_traced_through_every_mutation() -> None:
    path = MODULES / "incremental.py"
    assert list(analyze_module(path).keys) == sorted(
        [
            _key(path, "backup_file", 'result["backup_file"]', T.STR, "changed"),
            _key(path, "changed", "result = dict(changed=False", T.BOOL),
            _key(path, "changed", 'result["changed"] = True', T.BOOL, "changed"),
            _key(path, "checksum", "result.update(mode=", T.STR),
            _key(path, "mode", "result.update(mode=", T.STR),
            _key(path, "owner", 'result.setdefault("owner"', T.STR),
            _key(path, "path", "result = dict(changed=False", None),
            _key(path, "size", 'result["size"] = 42', T.INT),
        ],
        key=lambda k: (k.path, k.line),
    )


def test_keyword_arguments_to_exit_json() -> None:
    path = MODULES / "keywords.py"
    keys = {
        k.path: k for k in analyze_module(path).keys if k.outcome is Outcome.SUCCESS
    }
    assert {name: k.inferred_type for name, k in keys.items()} == {
        "changed": T.BOOL,
        "count": T.INT,
        "enabled": T.BOOL,
        "items": T.LIST,
        "ping": T.STR,
        "ratio": T.FLOAT,
    }
    assert keys["ping"].line == _line(path, 'ping="pong"')
    assert all(k.outcome is Outcome.SUCCESS for k in keys.values())


def test_named_dict_value_gives_nested_keys() -> None:
    path = MODULES / "nested.py"
    assert list(analyze_module(path).keys) == [
        _key(path, "changed", "exit_json(changed=False", T.BOOL),
        _key(path, "info", "exit_json(changed=False", T.DICT),
        _key(path, "info.exists", 'info = {"exists": True}', T.BOOL),
        _key(path, "info.owner", 'info["owner"]', T.STR),
        _key(path, "info.size", 'info["size"]', T.INT),
    ]


@pytest.mark.parametrize(
    ("module", "expected"),
    [
        ("hybrid", {"changed": T.BOOL, "checksum": T.STR, "dest": None}),
        ("sidecar", {"changed": T.BOOL, "features": T.LIST, "version": T.STR}),
        ("invalid", {"changed": T.BOOL, "value": T.STR}),
        ("raises_on_import", {"changed": T.BOOL, "trapped": T.BOOL}),
        ("virtual", {}),
        ("include_tasks", {}),
    ],
)
def test_other_fixtures(module: str, expected: dict[str, ReturnType | None]) -> None:
    keys = analyze_module(MODULES / f"{module}.py").keys
    assert {k.path: k.inferred_type for k in keys} == expected


def test_helper_and_dynamic_fixtures_resolve_through_helpers_and_wrappers() -> None:
    # Plan task 14: helpers and wrappers are followed one level deep.
    helper = [k.path for k in analyze_module(MODULES / "helper.py").keys]
    assert helper == ["changed", "name", "owner", "state"]
    dynamic = [k.path for k in analyze_module(MODULES / "dynamic.py").keys]
    assert dynamic == ["changed", "status"]


def test_import_trap_is_never_imported() -> None:
    before = set(sys.modules)
    analyze_module(MODULES / "raises_on_import.py")
    assert not {m for m in set(sys.modules) - before if "raises_on_import" in m}


# --- tracing rules (tmp modules) ------------------------------------------------------


def test_dict_literal_and_update_with_a_dict_literal(tmp_path: Path) -> None:
    _, keys = _analyze_source(
        tmp_path,
        "def main():\n"
        "    result = {'a': 1}\n"
        "    result.update({'b': 'x'})\n"
        "    result.update(dict(c=True))\n"
        "    module.exit_json(**result)\n",
    )
    assert [(k.path, k.inferred_type, k.line) for k in keys] == [
        ("a", T.INT, 2),
        ("b", T.STR, 3),
        ("c", T.BOOL, 4),
    ]


def test_augmented_subscript_assignment(tmp_path: Path) -> None:
    _, keys = _analyze_source(
        tmp_path,
        "def main():\n    r = {}\n    r['n'] += 1\n    m.exit_json(**r)\n",
    )
    assert [(k.path, k.inferred_type) for k in keys] == [("n", T.INT)]


def test_setdefault_without_a_default_is_unknown(tmp_path: Path) -> None:
    _, keys = _analyze_source(
        tmp_path,
        "def main():\n    r = {}\n    r.setdefault('x')\n    m.exit_json(**r)\n",
    )
    assert [(k.path, k.inferred_type) for k in keys] == [("x", None)]


def test_nested_dict_literal_value_gives_nested_keys_with_their_lines(
    tmp_path: Path,
) -> None:
    _, keys = _analyze_source(
        tmp_path,
        "def main():\n"
        "    m.exit_json(\n"
        "        outer={\n"
        "            'inner': 1,\n"
        "            'deep': {'x': 'y'},\n"
        "        },\n"
        "    )\n",
    )
    assert [(k.path, k.inferred_type, k.line) for k in keys] == [
        ("outer", T.DICT, 3),
        ("outer.deep", T.DICT, 5),
        ("outer.deep.x", T.STR, 5),
        ("outer.inner", T.INT, 4),
    ]


def test_scalar_name_takes_the_type_of_its_only_assignment(tmp_path: Path) -> None:
    _, keys = _analyze_source(
        tmp_path,
        "def main():\n"
        "    msg = 'pong'\n"
        "    n = 1\n"
        "    n = 'two'\n"
        "    m.exit_json(ping=msg, n=n)\n",
    )
    assert [(k.path, k.inferred_type) for k in keys] == [("n", None), ("ping", T.STR)]


def test_tracing_stays_inside_the_function(tmp_path: Path) -> None:
    _, keys = _analyze_source(
        tmp_path,
        "result = {'module_level': 1}\n"
        "def other():\n"
        "    result = {'elsewhere': 1}\n"
        "def main():\n"
        "    result = {'here': 1}\n"
        "    def inner():\n"
        "        result['nested_scope'] = 2\n"
        "    m.exit_json(**result)\n",
    )
    assert [k.path for k in keys] == ["here"]


def test_conditions_come_from_where_each_key_is_set(tmp_path: Path) -> None:
    _, keys = _analyze_source(
        tmp_path,
        "def main():\n"
        "    r = {}\n"
        "    if state == 'present':\n"
        "        r['created'] = True\n"
        "    else:\n"
        "        r['removed'] = True\n"
        "    if done:\n"
        "        m.exit_json(extra=1, **r)\n",
    )
    assert [(k.path, k.condition) for k in keys] == [
        ("created", "state == 'present'"),
        ("extra", "done"),
        ("removed", "not (state == 'present')"),
    ]


def test_every_exit_json_call_counts(tmp_path: Path) -> None:
    _, keys = _analyze_source(
        tmp_path,
        "def main():\n"
        "    if early:\n"
        "        self.module.exit_json(skipped_reason='early')\n"
        "    module.exit_json(done=True)\n",
    )
    assert [(k.path, k.condition) for k in keys] == [
        ("done", None),
        ("skipped_reason", "early"),
    ]


def test_module_level_exit_json(tmp_path: Path) -> None:
    _, keys = _analyze_source(tmp_path, "r = {'a': 1}\nmodule.exit_json(**r)\n")
    assert [k.path for k in keys] == ["a"]


def test_self_referencing_dict_does_not_loop(tmp_path: Path) -> None:
    _, keys = _analyze_source(
        tmp_path,
        "def main():\n    r = {}\n    r['me'] = r\n    m.exit_json(r=r)\n",
    )
    assert [k.path for k in keys] == ["r", "r.me"]


def test_untraceable_parts_are_left_out_of_keys(tmp_path: Path) -> None:
    # Plan task 14 reports these as unresolved; they never become keys.
    _, keys = _analyze_source(
        tmp_path,
        "def main(extra):\n"
        "    r = {'ok': 1}\n"
        "    r[name] = 2\n"
        "    r.update(other)\n"
        "    m.exit_json(**r, **extra, **m.params)\n",
    )
    assert [k.path for k in keys] == ["ok"]


def test_exact_duplicates_are_reported_once(tmp_path: Path) -> None:
    _, keys = _analyze_source(
        tmp_path,
        "def main():\n    r = {'a': 1}\n    m.exit_json(**r)\n    m.exit_json(**r)\n",
    )
    assert [k.path for k in keys] == ["a"]


def test_no_exit_json_means_no_keys(tmp_path: Path) -> None:
    _, keys = _analyze_source(tmp_path, "x = {'a': 1}\n")
    assert keys == []


def test_syntax_error_raises(tmp_path: Path) -> None:
    path = tmp_path / "broken.py"
    path.write_text("def main(:\n")
    with pytest.raises(AnalysisError, match="SyntaxError"):
        analyze_module(path)


def test_unreadable_file_raises(tmp_path: Path) -> None:
    with pytest.raises(AnalysisError, match=re.escape("missing.py")):
        analyze_module(tmp_path / "missing.py")


def test_keys_that_cannot_be_path_segments_are_skipped(tmp_path: Path) -> None:
    _, keys = _analyze_source(
        tmp_path,
        "def main():\n"
        "    r = {'': 1, 'a.b': 2, 'ok': 3}\n"
        "    r[''] = 4\n"
        "    m.exit_json(**r)\n",
    )
    assert [k.path for k in keys] == ["ok"]
