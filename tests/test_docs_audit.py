"""Tests for the RETURN documentation audit (plan task 10: FR-5, FR-6)."""

import re
import sys
from pathlib import Path

import pytest

from dr_ansible.config import Config
from dr_ansible.discovery import detect_project, discover_modules
from dr_ansible.docs_audit import DocsAuditError, audit_docs
from dr_ansible.model import Language, ModuleInfo, ReturnStatus

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CORE = FIXTURES / "core"

VALID_RETURN = '''RETURN = r"""
value:
    description: A value.
    returned: always
    type: str
"""
'''


def _fixture(root: Path, name: str) -> ModuleInfo:
    modules = {m.name: m for m in discover_modules(detect_project(root))}
    return modules[name]


def _tmp_module(
    tmp_path: Path, source: str, *, name: str = "thing", sidecar: str | None = None
) -> ModuleInfo:
    path = tmp_path / f"{name}.py"
    path.write_text(source)
    sidecar_path = None
    if sidecar is not None:
        sidecar_path = tmp_path / f"{name}.yml"
        sidecar_path.write_text(sidecar)
    return ModuleInfo(
        name=name,
        fqcn=f"ns.coll.{name}",
        language=Language.PYTHON,
        module_path=path,
        sidecar_path=sidecar_path,
    )


# --- the core fixture tree (FR-5, FR-6) --------------------------------------------


@pytest.mark.parametrize(
    ("name", "status"),
    [
        ("dynamic", ReturnStatus.MISSING),
        ("helper", ReturnStatus.PRESENT),
        ("hybrid", ReturnStatus.PRESENT),
        ("include_tasks", ReturnStatus.EXEMPT),
        ("incremental", ReturnStatus.MISSING),
        ("invalid", ReturnStatus.INVALID),
        ("keywords", ReturnStatus.PLACEHOLDER),
        ("nested", ReturnStatus.PRESENT),
        ("raises_on_import", ReturnStatus.MISSING),
        ("sidecar", ReturnStatus.PRESENT),
        ("virtual", ReturnStatus.MISSING),
    ],
)
def test_core_fixture_statuses(name: str, status: ReturnStatus) -> None:
    assert audit_docs(_fixture(CORE, name), Config()).status is status


def test_collection_fixture_statuses() -> None:
    widget = _fixture(FIXTURES / "collection", "widget")
    gadget = _fixture(FIXTURES / "built_collection", "gadget")
    assert audit_docs(widget, Config()).status is ReturnStatus.PRESENT
    assert audit_docs(gadget, Config()).status is ReturnStatus.MISSING


def test_placeholder_keeps_its_raw_text() -> None:
    result = audit_docs(_fixture(CORE, "keywords"), Config())
    assert result.raw_text == "#"


def test_present_keeps_its_raw_text_exactly() -> None:
    result = audit_docs(_fixture(CORE, "helper"), Config())
    assert result.raw_text is not None
    assert result.raw_text.startswith("\nname:\n    description: The name that was")
    assert "legacy_id:" in result.raw_text


def test_missing_has_no_raw_text_or_problems() -> None:
    result = audit_docs(_fixture(CORE, "incremental"), Config())
    assert result.raw_text is None
    assert result.problems == ()


def test_invalid_reports_the_yaml_error() -> None:
    result = audit_docs(_fixture(CORE, "invalid"), Config())
    assert result.raw_text is not None
    (problem,) = result.problems
    assert problem.startswith("RETURN is not valid YAML")


def test_sidecar_docs_are_used() -> None:
    result = audit_docs(_fixture(CORE, "sidecar"), Config())
    assert result.status is ReturnStatus.PRESENT
    assert result.raw_text is None  # the RETURN text lives inside the sidecar YAML


def test_import_trap_is_never_imported() -> None:
    before = set(sys.modules)
    result = audit_docs(_fixture(CORE, "raises_on_import"), Config())
    assert result.status is ReturnStatus.MISSING
    assert not {m for m in set(sys.modules) - before if "raises_on_import" in m}


def test_audit_prints_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    for module in discover_modules(detect_project(CORE)):
        if module.language is Language.PYTHON:
            audit_docs(module, Config())
    assert capsys.readouterr() == ("", "")


# --- exemption (FR-6) --------------------------------------------------------------


def test_exempt_needs_the_module_on_the_allowlist() -> None:
    config = Config(exempt_modules=frozenset({"meta"}))
    result = audit_docs(_fixture(CORE, "include_tasks"), config)
    assert result.status is ReturnStatus.MISSING


def test_exempt_matches_any_module_name(tmp_path: Path) -> None:
    module = _tmp_module(tmp_path, "")
    config = Config(exempt_modules=frozenset({"ns.coll.thing"}))
    assert audit_docs(module, config).status is ReturnStatus.EXEMPT


def test_exempt_covers_placeholders(tmp_path: Path) -> None:
    module = _tmp_module(tmp_path, 'RETURN = r"""#"""\n')
    config = Config(exempt_modules=frozenset({"thing"}))
    assert audit_docs(module, config).status is ReturnStatus.EXEMPT


def test_exempt_does_not_hide_present_or_invalid_docs(tmp_path: Path) -> None:
    config = Config(exempt_modules=frozenset({"thing"}))
    present = _tmp_module(tmp_path, VALID_RETURN)
    assert audit_docs(present, config).status is ReturnStatus.PRESENT
    invalid = _tmp_module(tmp_path, 'RETURN = r"""\nx: [unclosed\n"""\n', name="bad")
    config = Config(exempt_modules=frozenset({"bad"}))
    assert audit_docs(invalid, config).status is ReturnStatus.INVALID


# --- edge cases in .py files ---------------------------------------------------------


def test_empty_string_is_a_placeholder(tmp_path: Path) -> None:
    module = _tmp_module(tmp_path, 'RETURN = r"""\n"""\n')
    assert audit_docs(module, Config()).status is ReturnStatus.PLACEHOLDER


def test_annotated_assignment_is_ignored_like_ansible_does(tmp_path: Path) -> None:
    # ansible-core's read_docstring only sees plain `RETURN = ...` assignments.
    module = _tmp_module(tmp_path, VALID_RETURN.replace("RETURN =", "RETURN: str ="))
    result = audit_docs(module, Config())
    assert result.status is ReturnStatus.MISSING
    assert result.problems == (
        "RETURN uses an annotated assignment, which ansible-core ignores",
    )


def test_last_assignment_wins(tmp_path: Path) -> None:
    module = _tmp_module(tmp_path, 'RETURN = r"""#"""\n' + VALID_RETURN)
    result = audit_docs(module, Config())
    assert result.status is ReturnStatus.PRESENT
    assert result.raw_text is not None
    assert "value:" in result.raw_text


def test_scalar_return_is_invalid(tmp_path: Path) -> None:
    module = _tmp_module(tmp_path, 'RETURN = "just a string"\n')
    result = audit_docs(module, Config())
    assert result.status is ReturnStatus.INVALID
    assert result.problems == ("RETURN must be a YAML mapping, got str",)


def test_non_literal_return_is_invalid(tmp_path: Path) -> None:
    module = _tmp_module(tmp_path, 'RETURN = "a" + "b"\n')
    result = audit_docs(module, Config())
    assert result.status is ReturnStatus.INVALID
    assert result.problems == ("RETURN is not a plain string literal",)


def test_broken_documentation_does_not_spoil_a_valid_return(tmp_path: Path) -> None:
    source = 'DOCUMENTATION = r"""\nmodule: [broken\n"""\n' + VALID_RETURN
    result = audit_docs(_tmp_module(tmp_path, source), Config())
    assert result.status is ReturnStatus.PRESENT
    (problem,) = result.problems
    assert problem.startswith("other documentation failed to parse")


def test_broken_documentation_with_missing_return(tmp_path: Path) -> None:
    source = 'DOCUMENTATION = r"""\nmodule: [broken\n"""\n'
    result = audit_docs(_tmp_module(tmp_path, source), Config())
    assert result.status is ReturnStatus.MISSING
    assert len(result.problems) == 1


def test_python_syntax_error_raises(tmp_path: Path) -> None:
    module = _tmp_module(tmp_path, "x = (\n")
    with pytest.raises(DocsAuditError, match="SyntaxError"):
        audit_docs(module, Config())


def test_unreadable_file_raises(tmp_path: Path) -> None:
    module = _tmp_module(tmp_path, "")
    module.module_path.unlink()
    with pytest.raises(DocsAuditError, match=re.escape("thing.py")):
        audit_docs(module, Config())


def test_powershell_modules_are_not_audited(tmp_path: Path) -> None:
    module = ModuleInfo(
        name="win_x",
        fqcn="ns.coll.win_x",
        language=Language.POWERSHELL,
        module_path=tmp_path / "win_x.ps1",
    )
    with pytest.raises(ValueError, match="PowerShell"):
        audit_docs(module, Config())


# --- edge cases in sidecar files ----------------------------------------------------


SIDECAR_DOC = "DOCUMENTATION:\n  module: thing\n"


def test_sidecar_without_return_is_missing(tmp_path: Path) -> None:
    module = _tmp_module(tmp_path, "", sidecar=SIDECAR_DOC)
    assert audit_docs(module, Config()).status is ReturnStatus.MISSING


def test_sidecar_with_null_return_is_a_placeholder(tmp_path: Path) -> None:
    module = _tmp_module(tmp_path, "", sidecar=SIDECAR_DOC + "RETURN:\n")
    assert audit_docs(module, Config()).status is ReturnStatus.PLACEHOLDER


def test_unparsable_sidecar_is_invalid(tmp_path: Path) -> None:
    module = _tmp_module(tmp_path, "", sidecar="RETURN: [unclosed\n")
    result = audit_docs(module, Config())
    assert result.status is ReturnStatus.INVALID
    assert result.problems[0].startswith("sidecar is not valid YAML")


def test_sidecar_wins_over_docs_in_the_python_file(tmp_path: Path) -> None:
    module = _tmp_module(tmp_path, VALID_RETURN, sidecar=SIDECAR_DOC)
    assert audit_docs(module, Config()).status is ReturnStatus.MISSING
