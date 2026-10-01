"""Tests for type inference and condition capture (plan task 12: FR-11)."""

import ast

import pytest

from dr_ansible.model import ReturnType
from dr_ansible.static.infer import Inferred, condition_for, infer, parent_map

T = ReturnType


def _infer(expr: str) -> Inferred:
    return infer(ast.parse(expr, mode="eval").body)


# --- scalars --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("True", T.BOOL),
        ("False", T.BOOL),
        ("3", T.INT),
        ("-3", T.INT),
        ("0.5", T.FLOAT),
        ("-0.5", T.FLOAT),
        ("'pong'", T.STR),
        ("b'raw'", T.STR),
        ('f"sha1:{path}"', T.STR),
        ("'a' 'b'", T.STR),
    ],
)
def test_literals(expr: str, expected: ReturnType) -> None:
    assert _infer(expr) == Inferred(expected)


@pytest.mark.parametrize("expr", ["None", "path", "module.params", "x[0]", "x.y.z"])
def test_unknown(expr: str) -> None:
    assert _infer(expr) == Inferred(None)


# --- containers -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expr", "elements"),
    [
        ("['a', 'b']", T.STR),
        ("[1, 2]", T.INT),
        ("(1, 2)", T.INT),
        ("{True, False}", T.BOOL),
        ("[]", None),
        ("['a', 1]", None),
        ("['a', name]", None),
        ("[{'k': 1}]", T.DICT),
    ],
)
def test_sequences_are_lists_with_common_element_type(
    expr: str, elements: ReturnType | None
) -> None:
    assert _infer(expr) == Inferred(T.LIST, elements=elements)


def test_dict_literal_carries_nested_keys_sorted() -> None:
    assert _infer("{'size': 1, 'exists': True, 'owner': name}") == Inferred(
        T.DICT,
        children=(
            ("exists", Inferred(T.BOOL)),
            ("owner", Inferred(None)),
            ("size", Inferred(T.INT)),
        ),
    )


def test_nested_dicts_nest() -> None:
    inferred = _infer("{'outer': {'inner': 'x'}}")
    assert inferred.children == (
        ("outer", Inferred(T.DICT, children=(("inner", Inferred(T.STR)),))),
    )


def test_dict_literal_later_key_wins() -> None:
    assert _infer("{'a': 1, 'a': 'x'}").children == (("a", Inferred(T.STR)),)


def test_dict_with_computed_or_unpacked_keys_keeps_only_constant_keys() -> None:
    inferred = _infer("{'a': 1, name: 2, **extra}")
    assert inferred == Inferred(T.DICT, children=(("a", Inferred(T.INT)),))


def test_dict_call_with_keywords() -> None:
    assert _infer("dict(changed=False, path=p, size=1)") == Inferred(
        T.DICT,
        children=(
            ("changed", Inferred(T.BOOL)),
            ("path", Inferred(None)),
            ("size", Inferred(T.INT)),
        ),
    )


def test_dict_call_with_positional_argument_has_no_known_children() -> None:
    assert _infer("dict(other, a=1)") == Inferred(
        T.DICT, children=(("a", Inferred(T.INT)),)
    )


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("[x for x in y]", Inferred(T.LIST)),
        ("[str(x) for x in y]", Inferred(T.LIST, elements=T.STR)),
        ("{x for x in y}", Inferred(T.LIST)),
        ("{k: v for k, v in y}", Inferred(T.DICT)),
    ],
)
def test_comprehensions(expr: str, expected: Inferred) -> None:
    assert _infer(expr) == expected


# --- calls ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("str(x)", Inferred(T.STR)),
        ("repr(x)", Inferred(T.STR)),
        ("int(x)", Inferred(T.INT)),
        ("len(x)", Inferred(T.INT)),
        ("float(x)", Inferred(T.FLOAT)),
        ("bool(x)", Inferred(T.BOOL)),
        ("list(x)", Inferred(T.LIST)),
        ("sorted(x)", Inferred(T.LIST)),
        ("tuple(x)", Inferred(T.LIST)),
        ("set(x)", Inferred(T.LIST)),
        ("to_text(x)", Inferred(T.STR)),
        ("to_native(x, errors='surrogate_or_strict')", Inferred(T.STR)),
        ("os.path.join(a, b)", Inferred(T.STR)),
        ("os.path.basename(p)", Inferred(T.STR)),
        ("os.path.exists(p)", Inferred(T.BOOL)),
        ("'{0}'.format(x)", Inferred(T.STR)),
        ("', '.join(items)", Inferred(T.STR)),
        ("out.strip()", Inferred(T.STR)),
        ("name.lower()", Inferred(T.STR)),
        ("out.split()", Inferred(T.LIST, elements=T.STR)),
        ("out.splitlines()", Inferred(T.LIST, elements=T.STR)),
        ("module.run_command(cmd)", Inferred(None)),
        ("helper(x)", Inferred(None)),
    ],
)
def test_calls(expr: str, expected: Inferred) -> None:
    assert _infer(expr) == expected


# --- operators ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("'a' + 'b'", T.STR),
        ("'a' + name", T.STR),
        ("'%s' % x", T.STR),
        ("1 + 2", T.INT),
        ("1 + 2.0", T.FLOAT),
        ("1 / 2", T.FLOAT),
        ("7 // 2", T.INT),
        ("[1] + [2]", T.LIST),
        ("a == b", T.BOOL),
        ("a is None", T.BOOL),
        ("a in b", T.BOOL),
        ("not a", T.BOOL),
        ("a and b", None),
        ("'x' or 'y'", T.STR),
        ("a + b", None),
        ("'yes' if a else 'no'", T.STR),
        ("'yes' if a else 1", None),
    ],
)
def test_operators(expr: str, expected: ReturnType | None) -> None:
    assert _infer(expr).type is expected


def test_inferred_is_immutable_and_hashable() -> None:
    inferred = _infer("{'a': [1]}")
    assert hash(inferred) == hash(_infer("{'a': [1]}"))


# --- conditions -----------------------------------------------------------------------


def _condition_of(source: str, marker: str = "TARGET") -> str | None:
    tree = ast.parse(source)
    parents = parent_map(tree)
    (node,) = [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == marker]
    return condition_for(node, parents)


def test_no_condition_at_function_level() -> None:
    assert _condition_of("def main():\n    result['k'] = TARGET\n") is None


def test_if_body_gives_the_test() -> None:
    src = "def main():\n    if changed:\n        result['k'] = TARGET\n"
    assert _condition_of(src) == "changed"


def test_else_branch_negates_the_test() -> None:
    src = "def main():\n    if changed:\n        pass\n    else:\n        r = TARGET\n"
    assert _condition_of(src) == "not changed"


def test_else_of_a_negated_test_drops_the_not() -> None:
    src = "def main():\n    if not ok:\n        pass\n    else:\n        r = TARGET\n"
    assert _condition_of(src) == "ok"


def test_else_of_a_compound_test_is_parenthesised() -> None:
    src = "def main():\n    if a and b:\n        pass\n    else:\n        r = TARGET\n"
    assert _condition_of(src) == "not (a and b)"


def test_nested_ifs_join_outermost_first() -> None:
    src = (
        "def main():\n"
        "    if module.check_mode is False:\n"
        "        if state == 'present':\n"
        "            r = TARGET\n"
    )
    assert _condition_of(src) == "module.check_mode is False and state == 'present'"


def test_elif_chain() -> None:
    src = "def main():\n    if a:\n        pass\n    elif b:\n        r = TARGET\n"
    assert _condition_of(src) == "not a and b"


def test_condition_in_the_test_itself_is_not_its_own_condition() -> None:
    src = "def main():\n    if TARGET:\n        pass\n"
    assert _condition_of(src) is None


def test_conditions_stop_at_the_enclosing_function() -> None:
    src = "if outer:\n    def main():\n        if inner:\n            r = TARGET\n"
    assert _condition_of(src) == "inner"


def test_loops_and_try_blocks_add_no_condition() -> None:
    src = (
        "def main():\n"
        "    for x in y:\n"
        "        try:\n"
        "            r = TARGET\n"
        "        except Exception:\n"
        "            pass\n"
    )
    assert _condition_of(src) is None


def test_and_or_chains_are_parenthesised_when_joined() -> None:
    src = "def main():\n    if c:\n        if a or b:\n            r = TARGET\n"
    assert _condition_of(src) == "c and (a or b)"


def test_single_and_or_chain_is_not_parenthesised() -> None:
    src = "def main():\n    if a or b:\n        r = TARGET\n"
    assert _condition_of(src) == "a or b"
