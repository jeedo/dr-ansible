"""The helper that pastes a draft into a module, for AC-8 (plan task 32)."""

import ast

from tests.acceptance.paste import paste_draft

DRAFT = 'RETURN = r"""\nx:\n    description: d\n"""\n'


def _assigned(source: str) -> list[str]:
    return [
        t.id
        for node in ast.parse(source).body
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name)
    ]


def test_goes_after_examples() -> None:
    source = (
        "#!/usr/bin/python\n"
        "from __future__ import annotations\n"
        'DOCUMENTATION = """\nd\n"""\n'
        'EXAMPLES = """\ne\n"""\n'
        "import os\n"
    )
    pasted = paste_draft(source, DRAFT)
    assert _assigned(pasted) == ["DOCUMENTATION", "EXAMPLES", "RETURN"]
    assert pasted.startswith(source[: source.index("import os")])
    assert pasted.endswith("import os\n")


def test_goes_after_documentation_without_examples() -> None:
    source = 'DOCUMENTATION = """\nd\n"""\nimport os\n'
    assert _assigned(paste_draft(source, DRAFT)) == ["DOCUMENTATION", "RETURN"]


def test_goes_after_future_imports_without_docs() -> None:
    source = (
        "# comment\n"
        "from __future__ import annotations\n"
        "from __future__ import division\n"
        "import os\n"
    )
    pasted = paste_draft(source, DRAFT)
    tree = ast.parse(pasted)  # still valid: __future__ imports stay first
    assert isinstance(tree.body[0], ast.ImportFrom)
    assert isinstance(tree.body[1], ast.ImportFrom)
    assert _assigned(pasted) == ["RETURN"]


def test_goes_after_the_docstring_without_anything_else() -> None:
    source = '"""Module docstring."""\nimport os\n'
    pasted = paste_draft(source, DRAFT)
    assert ast.get_docstring(ast.parse(pasted)) == "Module docstring."
    assert _assigned(pasted) == ["RETURN"]


def test_goes_at_the_top_of_a_bare_file() -> None:
    assert _assigned(paste_draft("import os\n", DRAFT)) == ["RETURN"]


def test_the_original_text_is_kept() -> None:
    source = 'DOCUMENTATION = """\nd\n"""\n\n# a comment\nimport os\n'
    pasted = paste_draft(source, DRAFT)
    assert pasted.replace("\n\n" + DRAFT, "", 1) == source
