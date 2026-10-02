"""Keys set in loops over literal sequences are resolved exactly (FR-11, FR-12).

``for perm in [('readable', ...), ...]: output[perm[0]] = ...`` names each key
in the source, so unrolling it is exact, not a guess. Anything less certain
stays an unresolved "computed key name".
"""

from pathlib import Path

from dr_ansible.static.module_analyzer import ModuleAnalysis, analyze_module


def _analyze(tmp_path: Path, body: str) -> ModuleAnalysis:
    path = tmp_path / "thing.py"
    path.write_text("def main():\n    out = {}\n" + body + "    m.exit_json(**out)\n")
    return analyze_module(path)


def _keys(analysis: ModuleAnalysis) -> dict[str, int]:
    return {k.path: k.line for k in analysis.keys}


def _reasons(analysis: ModuleAnalysis) -> list[tuple[int, str]]:
    return [(u.line, u.reason) for u in analysis.unresolved]


def test_loop_over_a_list_of_strings(tmp_path: Path) -> None:
    analysis = _analyze(
        tmp_path,
        "    for name in ['version', 'attributes', 'attr_flags']:\n"
        "        out[name] = get(name)\n",
    )
    assert _keys(analysis) == {"version": 4, "attributes": 4, "attr_flags": 4}
    assert _reasons(analysis) == []


def test_loop_over_tuples_indexed_by_position(tmp_path: Path) -> None:
    # stat.py: for perm in [('readable', os.R_OK), ...]: output[perm[0]] = ...
    analysis = _analyze(
        tmp_path,
        "    for perm in [('readable', R), ('writeable', W), ('executable', X)]:\n"
        "        out[perm[0]] = access(perm[1])\n",
    )
    assert set(_keys(analysis)) == {"readable", "writeable", "executable"}
    assert _reasons(analysis) == []


def test_loop_with_tuple_unpacking(tmp_path: Path) -> None:
    analysis = _analyze(
        tmp_path,
        "    for attr, name in (('st_blocks', 'blocks'), ('st_rdev', 'rdev')):\n"
        "        if hasattr(st, attr):\n"
        "            out[name] = getattr(st, attr)\n",
    )
    keys = {k.path: k for k in analysis.keys}
    assert set(keys) == {"blocks", "rdev"}
    assert keys["blocks"].condition == "hasattr(st, attr)"


def test_loop_over_a_set_and_setdefault(tmp_path: Path) -> None:
    analysis = _analyze(
        tmp_path,
        "    for name in {'a', 'b'}:\n        out.setdefault(name, 0)\n",
    )
    assert set(_keys(analysis)) == {"a", "b"}


def test_nested_loops(tmp_path: Path) -> None:
    analysis = _analyze(
        tmp_path,
        "    for outer in ['x']:\n"
        "        for inner in ['y', 'z']:\n"
        "            out[inner] = outer\n",
    )
    assert set(_keys(analysis)) == {"y", "z"}


def test_unusable_or_non_string_items_leave_it_unresolved(tmp_path: Path) -> None:
    analysis = _analyze(
        tmp_path,
        "    for name in ['ok', 3]:\n"
        "        out[name] = 1\n"
        "    for name in ['a.b']:\n"
        "        out[name] = 1\n"
        "    for name in names:\n"
        "        out[name] = 1\n"
        "    for perm in [('a', 1), ('b',)]:\n"
        "        out[perm[1]] = 1\n",
    )
    assert _keys(analysis) == {}
    assert _reasons(analysis) == [
        (4, "computed key name"),
        (6, "computed key name"),
        (8, "computed key name"),
        (10, "computed key name"),
    ]


def test_other_expressions_of_the_loop_variable_stay_unresolved(tmp_path: Path) -> None:
    analysis = _analyze(
        tmp_path,
        "    for name in ['a', 'b']:\n"
        "        out[name + '_x'] = 1\n"
        "        out[name.upper()] = 1\n",
    )
    assert _keys(analysis) == {}
    assert _reasons(analysis) == [(4, "computed key name"), (5, "computed key name")]


def test_a_loop_variable_rebound_in_the_loop_is_not_trusted(tmp_path: Path) -> None:
    analysis = _analyze(
        tmp_path,
        "    for name in ['a', 'b']:\n"
        "        name = name + '_x'\n"
        "        out[name] = 1\n",
    )
    assert _keys(analysis) == {}
    assert _reasons(analysis) == [(5, "computed key name")]


def test_a_loop_in_another_function_does_not_count(tmp_path: Path) -> None:
    path = tmp_path / "thing.py"
    path.write_text(
        "def main():\n"
        "    out = {}\n"
        "    for name in ['a']:\n"
        "        def helper():\n"
        "            out[name] = 1\n"
        "    m.exit_json(**out)\n"
    )
    analysis = analyze_module(path)
    assert {k.path for k in analysis.keys} == set()
