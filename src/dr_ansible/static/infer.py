"""Infer return types from AST values and capture enclosing conditions (FR-11).

Both helpers are pure functions over ``ast`` nodes: nothing is imported or
evaluated. Inference is deliberately conservative. A value whose type cannot
be read off the expression itself (a variable, an attribute, an arbitrary
call) is unknown, never guessed; tracing variables back to their values is
the analysers' job.
"""

import ast
from collections.abc import Mapping
from dataclasses import dataclass

from dr_ansible.model import ReturnType

T = ReturnType


@dataclass(frozen=True, slots=True)
class Inferred:
    """An inferred type. ``type=None`` means unknown.

    ``elements`` is the common element type of a list, if every element
    agrees. ``children`` are the constant keys of a dict literal or
    ``dict(...)`` call, sorted by key, so nested keys can become dotted paths.
    """

    type: ReturnType | None
    elements: ReturnType | None = None
    children: tuple[tuple[str, "Inferred"], ...] = ()


UNKNOWN = Inferred(None)

#: Builtins and Ansible helpers whose result type is fixed, by called name.
_CALL_TYPES: dict[str, ReturnType] = {
    "str": T.STR,
    "repr": T.STR,
    "to_text": T.STR,
    "to_native": T.STR,
    "to_bytes": T.STR,
    "int": T.INT,
    "len": T.INT,
    "float": T.FLOAT,
    "bool": T.BOOL,
    "list": T.LIST,
    "sorted": T.LIST,
    "tuple": T.LIST,
    "set": T.LIST,
    "frozenset": T.LIST,
    "os.path.join": T.STR,
    "os.path.basename": T.STR,
    "os.path.dirname": T.STR,
    "os.path.abspath": T.STR,
    "os.path.realpath": T.STR,
    "os.path.normpath": T.STR,
    "os.path.expanduser": T.STR,
    "os.path.exists": T.BOOL,
    "os.path.isfile": T.BOOL,
    "os.path.isdir": T.BOOL,
    "os.path.islink": T.BOOL,
    "os.path.getsize": T.INT,
    "os.path.getmtime": T.FLOAT,
}

#: String methods, by method name, whatever they are called on.
_STR_METHODS = frozenset(
    {
        "capitalize",
        "format",
        "join",
        "lower",
        "lstrip",
        "replace",
        "rstrip",
        "strip",
        "title",
        "upper",
    }
)
_STR_LIST_METHODS = frozenset({"split", "rsplit", "splitlines"})
_BOOL_METHODS = frozenset({"startswith", "endswith"})
_NUMBERS = (T.INT, T.FLOAT)


def infer(node: ast.expr) -> Inferred:
    """Infer the type of the value ``node`` evaluates to."""
    match node:
        case ast.Constant(value=value):
            return _constant(value)
        case ast.JoinedStr():
            return Inferred(T.STR)
        case ast.List(elts=elts) | ast.Tuple(elts=elts) | ast.Set(elts=elts):
            return Inferred(T.LIST, elements=_common([infer(e).type for e in elts]))
        case ast.Dict(keys=keys, values=values):
            return Inferred(T.DICT, children=_children(zip(keys, values, strict=True)))
        case ast.ListComp(elt=elt) | ast.SetComp(elt=elt):
            return Inferred(T.LIST, elements=infer(elt).type)
        case ast.DictComp():
            return Inferred(T.DICT)
        case ast.Call():
            return _call(node)
        case ast.BinOp(left=left, op=op, right=right):
            return Inferred(_binop(infer(left).type, op, infer(right).type))
        case ast.UnaryOp(op=ast.Not()):
            return Inferred(T.BOOL)
        case ast.UnaryOp(op=ast.USub() | ast.UAdd(), operand=operand):
            inner = infer(operand).type
            return Inferred(inner if inner in _NUMBERS else None)
        case ast.Compare():
            return Inferred(T.BOOL)
        case ast.BoolOp(values=values):
            return Inferred(_same([infer(v).type for v in values]))
        case ast.IfExp(body=body, orelse=orelse):
            return Inferred(_same([infer(body).type, infer(orelse).type]))
    return UNKNOWN


def _constant(value: object) -> Inferred:
    # bool before int: True is an int in Python.
    if isinstance(value, bool):
        return Inferred(T.BOOL)
    if isinstance(value, int):
        return Inferred(T.INT)
    if isinstance(value, float):
        return Inferred(T.FLOAT)
    if isinstance(value, str | bytes):
        return Inferred(T.STR)
    return UNKNOWN  # None and anything else: unknown (FR-11)


def _children(
    pairs: "zip[tuple[ast.expr | None, ast.expr]]",
) -> tuple[tuple[str, Inferred], ...]:
    """Constant string keys of a dict literal; later duplicates win."""
    found: dict[str, Inferred] = {}
    for key, value in pairs:
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            found[key.value] = infer(value)
    return tuple(sorted(found.items()))


def _call(node: ast.Call) -> Inferred:
    name = _dotted_name(node.func)
    if name == "dict":
        found = {k.arg: infer(k.value) for k in node.keywords if k.arg is not None}
        return Inferred(T.DICT, children=tuple(sorted(found.items())))
    if name in _CALL_TYPES:
        return Inferred(_CALL_TYPES[name])
    if isinstance(node.func, ast.Attribute):
        method = node.func.attr
        if method in _STR_METHODS:
            return Inferred(T.STR)
        if method in _STR_LIST_METHODS:
            return Inferred(T.LIST, elements=T.STR)
        if method in _BOOL_METHODS:
            return Inferred(T.BOOL)
    return UNKNOWN


def _binop(
    left: ReturnType | None, op: ast.operator, right: ReturnType | None
) -> ReturnType | None:
    if isinstance(op, ast.Add):
        if T.STR in (left, right):
            return T.STR
        if T.LIST in (left, right):
            return T.LIST
    if isinstance(op, ast.Mod) and left is T.STR:
        return T.STR  # printf-style formatting
    if left in _NUMBERS and right in _NUMBERS:
        if isinstance(op, ast.Div) or T.FLOAT in (left, right):
            return T.FLOAT
        return T.INT
    return None


def _common(types: list[ReturnType | None]) -> ReturnType | None:
    """The single type every element shares, if there is one and it is known."""
    return _same(types) if types else None


def _same(types: list[ReturnType | None]) -> ReturnType | None:
    first = types[0]
    return first if all(t is first for t in types) else None


def _dotted_name(node: ast.expr) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


# --- enclosing conditions -------------------------------------------------------------


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
_SIMPLE_TESTS = (ast.Name, ast.Attribute, ast.Call, ast.Subscript, ast.Constant)


def parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    """Map every node in ``tree`` to its parent."""
    return {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }


def condition_for(node: ast.AST, parents: Mapping[ast.AST, ast.AST]) -> str | None:
    """The ``if`` tests ``node`` runs under, outermost first, as source text.

    A node in an ``if`` body adds the test; one in its ``else`` adds the
    negated test, so an ``elif`` chain reads ``not a and b``. Loops and
    ``try`` blocks add nothing, and the walk stops at the enclosing function.
    Returns ``None`` when the node is unconditional within its function.
    """
    conditions: list[tuple[str, bool]] = []  # (text, is a bare and/or chain)
    current = node
    while current in parents:
        parent = parents[current]
        if isinstance(parent, _SCOPES):
            break
        if isinstance(parent, ast.If):
            if any(current is stmt for stmt in parent.body):
                conditions.append(
                    (ast.unparse(parent.test), isinstance(parent.test, ast.BoolOp))
                )
            elif any(current is stmt for stmt in parent.orelse):
                conditions.append((_negate(parent.test), False))
        current = parent

    if not conditions:
        return None
    ordered = list(reversed(conditions))
    if len(ordered) == 1:
        return ordered[0][0]
    return " and ".join(f"({text})" if bare else text for text, bare in ordered)


def _negate(test: ast.expr) -> str:
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return ast.unparse(test.operand)
    text = ast.unparse(test)
    return f"not {text}" if isinstance(test, _SIMPLE_TESTS) else f"not ({text})"
