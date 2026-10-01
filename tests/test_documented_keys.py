"""Tests for documented-key flattening and field checks (plan task 11: FR-7)."""

from pathlib import Path

import pytest

from dr_ansible.config import Config
from dr_ansible.discovery import detect_project, discover_modules
from dr_ansible.docs_audit import audit_docs
from dr_ansible.model import (
    DocResult,
    DocumentedKey,
    Language,
    ModuleInfo,
    ReturnStatus,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CORE = FIXTURES / "core"


def _fixture(name: str, root: Path = CORE) -> DocResult:
    modules = {m.name: m for m in discover_modules(detect_project(root))}
    return audit_docs(modules[name], Config())


def _audit(tmp_path: Path, return_yaml: str) -> DocResult:
    path = tmp_path / "thing.py"
    path.write_text(f'RETURN = r"""\n{return_yaml}"""\n')
    module = ModuleInfo(
        name="thing",
        fqcn="ns.coll.thing",
        language=Language.PYTHON,
        module_path=path,
    )
    return audit_docs(module, Config())


# --- fixtures ------------------------------------------------------------------------


def test_helper_keys_are_listed_with_their_fields() -> None:
    result = _fixture("helper")
    assert result.keys == (
        DocumentedKey(
            path="legacy_id", type="int", returned="success", has_description=True
        ),
        DocumentedKey(path="name", type="str", returned="always", has_description=True),
        DocumentedKey(
            path="state", type="str", returned="success", has_description=True
        ),
    )
    assert result.problems == ()


def test_nested_contains_keys_are_flattened_to_dotted_paths() -> None:
    result = _fixture("nested")
    assert [k.path for k in result.keys] == [
        "info",
        "info.exists",
        "info.owner",
        "info.size",
    ]
    assert result.key("info.size") == DocumentedKey(
        path="info.size",
        type="int",
        returned="success, path exists",
        has_description=True,
    )
    assert result.problems == ()


def test_sidecar_keys_include_elements() -> None:
    result = _fixture("sidecar")
    assert result.key("features") == DocumentedKey(
        path="features",
        type="list",
        returned="always",
        has_description=True,
        elements="str",
    )


def test_collection_keys() -> None:
    result = _fixture("widget", FIXTURES / "collection")
    assert [k.path for k in result.keys] == ["widget_id"]


@pytest.mark.parametrize(
    "name", ["incremental", "keywords", "invalid", "include_tasks"]
)
def test_only_present_modules_have_keys(name: str) -> None:
    assert _fixture(name).keys == ()


# --- required fields (top level) ----------------------------------------------------


def test_missing_top_level_fields_are_problems(tmp_path: Path) -> None:
    result = _audit(
        tmp_path,
        "good:\n  description: ok\n  returned: always\n  type: str\n"
        "bare:\n  type: int\n",
    )
    assert result.status is ReturnStatus.PRESENT
    assert result.problems == (
        "bare: missing required field 'description'",
        "bare: missing required field 'returned'",
    )
    assert result.key("bare") == DocumentedKey(path="bare", type="int")


def test_empty_description_counts_as_missing(tmp_path: Path) -> None:
    result = _audit(
        tmp_path, "k:\n  description: ''\n  returned: always\n  type: str\n"
    )
    assert result.problems == ("k: missing required field 'description'",)
    key = result.key("k")
    assert key is not None
    assert key.has_description is False


def test_list_description_counts(tmp_path: Path) -> None:
    result = _audit(
        tmp_path,
        "k:\n  description:\n    - First line.\n    - Second.\n"
        "  returned: always\n  type: str\n",
    )
    assert result.problems == ()
    key = result.key("k")
    assert key is not None
    assert key.has_description is True


def test_nested_keys_do_not_need_returned(tmp_path: Path) -> None:
    result = _audit(
        tmp_path,
        "outer:\n  description: d\n  returned: always\n  type: complex\n"
        "  contains:\n    inner:\n      description: d\n      type: str\n",
    )
    assert result.problems == ()
    assert result.key("outer.inner") == DocumentedKey(
        path="outer.inner", type="str", has_description=True
    )


def test_deeply_nested_contains(tmp_path: Path) -> None:
    result = _audit(
        tmp_path,
        "a:\n  description: d\n  returned: always\n  type: complex\n  contains:\n"
        "    b:\n      description: d\n      type: dict\n      contains:\n"
        "        c:\n          description: d\n          type: bool\n",
    )
    assert [k.path for k in result.keys] == ["a", "a.b", "a.b.c"]


# --- type values (every level) -------------------------------------------------------


def test_unknown_type_is_a_problem_at_any_level(tmp_path: Path) -> None:
    result = _audit(
        tmp_path,
        "a:\n  description: d\n  returned: always\n  type: string\n"
        "b:\n  description: d\n  returned: always\n  type: complex\n  contains:\n"
        "    c:\n      description: d\n      type: dictionary\n",
    )
    assert result.problems == (
        "a: type 'string' is not one of bool, complex, dict, float, int, list, raw,"
        " str",
        "b.c: type 'dictionary' is not one of bool, complex, dict, float, int, list,"
        " raw, str",
    )
    key = result.key("a")
    assert key is not None
    assert key.type == "string"  # recorded as documented, problem reported


# --- malformed entries ----------------------------------------------------------------


def test_entry_that_is_not_a_mapping_is_skipped(tmp_path: Path) -> None:
    result = _audit(
        tmp_path,
        "good:\n  description: d\n  returned: always\n  type: str\nbad: just text\n",
    )
    assert [k.path for k in result.keys] == ["good"]
    assert result.problems == ("bad: entry must be a mapping, got str",)


def test_contains_that_is_not_a_mapping_is_a_problem(tmp_path: Path) -> None:
    result = _audit(
        tmp_path,
        "a:\n  description: d\n  returned: always\n  type: complex\n"
        "  contains: [x, y]\n",
    )
    assert [k.path for k in result.keys] == ["a"]
    assert result.problems == ("a: contains must be a mapping, got list",)


def test_key_name_with_a_dot_is_skipped(tmp_path: Path) -> None:
    result = _audit(
        tmp_path,
        "a.b:\n  description: d\n  returned: always\n  type: str\n"
        "c:\n  description: d\n  returned: always\n  type: str\n",
    )
    assert [k.path for k in result.keys] == ["c"]
    assert result.problems == ("a.b: key name contains '.'",)


def test_non_string_key_names_and_values_are_stringified(tmp_path: Path) -> None:
    result = _audit(tmp_path, "200:\n  description: d\n  returned: true\n  type: int\n")
    assert result.key("200") == DocumentedKey(
        path="200", type="int", returned="True", has_description=True
    )


def test_problems_are_sorted_by_key(tmp_path: Path) -> None:
    result = _audit(
        tmp_path,
        "zeta:\n  type: str\nalpha:\n  type: str\n",
    )
    assert [p.split(":", 1)[0] for p in result.problems] == [
        "alpha",
        "alpha",
        "zeta",
        "zeta",
    ]
