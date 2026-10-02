"""Paste a draft ``RETURN`` block into a module's source, as a human would (AC-8).

The draft goes after ``EXAMPLES``, the usual place in ansible-core, or after
``DOCUMENTATION`` if there is no ``EXAMPLES``. A module with neither (such as
``async_wrapper``) gets it after any ``from __future__`` imports, which must
stay first, or after the module docstring.
"""

import ast


def paste_draft(source: str, draft: str) -> str:
    """``source`` with ``draft`` inserted at the conventional place."""
    tree = ast.parse(source)
    line = _after(tree)
    lines = source.splitlines(keepends=True)
    if line and not lines[line - 1].endswith("\n"):
        lines[line - 1] += "\n"
    return "".join([*lines[:line], "\n\n" + draft, *lines[line:]])


def _after(tree: ast.Module) -> int:
    """The line after which the draft goes (0 for the top of the file)."""
    docs = {
        name: node.end_lineno or node.lineno
        for node in tree.body
        if isinstance(node, ast.Assign)
        for name in ("EXAMPLES", "DOCUMENTATION")
        if any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
    }
    if "EXAMPLES" in docs:
        return docs["EXAMPLES"]
    if "DOCUMENTATION" in docs:
        return docs["DOCUMENTATION"]
    future = [
        node.end_lineno or node.lineno
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "__future__"
    ]
    if future:
        return max(future)
    first = tree.body[0] if tree.body else None
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        return first.end_lineno or first.lineno
    return 0
