"""Tests for the report formats: table, markdown and JSON (plan task 21)."""

import json
from pathlib import Path
from typing import Any

import pytest

from dr_ansible.config import Config
from dr_ansible.model import (
    DocumentedKey,
    KeyReport,
    KeyStatus,
    Language,
    Location,
    ModuleInfo,
    ModuleReport,
    Observation,
    Outcome,
    ResultState,
    ReturnStatus,
    ReturnType,
    Sample,
    StaticKey,
    Unresolved,
)
from dr_ansible.report import (
    SCHEMA_VERSION,
    Counts,
    audit_markdown,
    audit_table,
    counts,
    keys_markdown,
    keys_table,
    reports_to_json,
)

ROOT = Path("/repo")
MODULES = ROOT / "lib" / "ansible" / "modules"


def _info(name: str, **kwargs: object) -> ModuleInfo:
    fields: dict[str, object] = {
        "name": name,
        "fqcn": f"ansible.builtin.{name}",
        "language": Language.PYTHON,
        "module_path": MODULES / f"{name}.py",
    }
    fields.update(kwargs)
    return ModuleInfo(**fields)  # type: ignore[arg-type]


def _static(path: str, line: int, name: str = "fetch", **kwargs: object) -> StaticKey:
    fields: dict[str, object] = {
        "path": path,
        "file": MODULES / f"{name}.py",
        "line": line,
        "outcome": Outcome.SUCCESS,
    }
    fields.update(kwargs)
    return StaticKey(**fields)  # type: ignore[arg-type]


def _fetch() -> ModuleReport:
    return ModuleReport(
        module=_info(
            "fetch",
            action_path=ROOT / "lib/ansible/plugins/action/fetch.py",
            test_target=ROOT / "test/integration/targets/fetch",
        ),
        return_status=ReturnStatus.MISSING,
        keys=(
            KeyReport(
                name="dest",
                status=KeyStatus.UNDOCUMENTED,
                static=(
                    _static("dest", 199, inferred_type=ReturnType.STR),
                    _static("dest", 208, condition="validate"),
                ),
                observed=Observation(
                    path="dest",
                    count=4,
                    types=frozenset({"str"}),
                    results=frozenset({ResultState.CHANGED, ResultState.OK}),
                    sample=Sample("/tmp/x"),
                    sources=(Location(ROOT / "test/t.yml", 3),),
                ),
            ),
            KeyReport(
                name="seen",
                status=KeyStatus.TEST_ONLY,
                observed=Observation(path="seen", count=1),
            ),
        ),
        unresolved=(
            Unresolved(file=MODULES / "fetch.py", line=12, reason="computed key name"),
        ),
        inherits_from=("ansible.legacy.slurp",),
    )


def _copy() -> ModuleReport:
    return ModuleReport(
        module=_info("copy"),
        return_status=ReturnStatus.PRESENT,
        keys=(
            KeyReport(
                name="checksum",
                status=KeyStatus.OK,
                documented=DocumentedKey(
                    path="checksum",
                    type="str",
                    returned="success",
                    has_description=True,
                ),
                static=(_static("checksum", 5, "copy"),),
            ),
            KeyReport(
                name="old",
                status=KeyStatus.STALE,
                documented=DocumentedKey(path="old", type="int"),
            ),
        ),
    )


def _broken() -> ModuleReport:
    return ModuleReport(
        module=_info("broken"),
        return_status=ReturnStatus.ERROR,
        error="SyntaxError: invalid syntax (line 3)",
    )


def _reports() -> list[ModuleReport]:
    return [_fetch(), _copy(), _broken()]


# --- counts -------------------------------------------------------------------------


def test_counts() -> None:
    assert counts(_fetch()) == Counts(
        documented=0, code=1, tests=2, undocumented=2, stale=0
    )
    assert counts(_copy()) == Counts(
        documented=2, code=1, tests=0, undocumented=0, stale=1
    )
    assert counts(_broken()) == Counts(0, 0, 0, 0, 0)


# --- audit table --------------------------------------------------------------------


def test_plain_audit_table_matches_the_documented_layout() -> None:
    assert audit_table(_reports(), use_rich=False) == (
        "MODULE                  RETURN   DOC  CODE  TESTS  UNDOC  STALE\n"
        "ansible.builtin.broken  error      0     0      0      0      0\n"
        "ansible.builtin.copy    present    2     1      0      0      1\n"
        "ansible.builtin.fetch   missing    0     1      2      2      0\n"
    )


def test_plain_audit_table_with_no_modules() -> None:
    assert audit_table([], use_rich=False) == (
        "MODULE  RETURN  DOC  CODE  TESTS  UNDOC  STALE\n"
    )


def test_rich_audit_table_has_the_same_cells() -> None:
    pytest.importorskip("rich")
    table = audit_table(_reports(), use_rich=True)
    assert "\x1b[" not in table  # no colour codes in captured output
    for cell in (
        "MODULE",
        "STALE",
        "ansible.builtin.fetch",
        "missing",
        "present",
        "error",
    ):
        assert cell in table


def test_audit_table_auto_detects_rich() -> None:
    pytest.importorskip("rich")
    assert audit_table(_reports()) == audit_table(_reports(), use_rich=True)


# --- keys table ---------------------------------------------------------------------


def test_plain_keys_table() -> None:
    assert keys_table(_fetch(), ROOT, use_rich=False) == (
        "ansible.builtin.fetch: RETURN missing\n"
        "inherits returns from: ansible.legacy.slurp\n"
        "\n"
        "KEY   STATUS        DOC  CODE                                  TESTS\n"
        "dest  undocumented  -    lib/ansible/modules/fetch.py:199,208      4\n"
        "seen  test-only     -    -                                         1\n"
        "\n"
        "unresolved:\n"
        "  lib/ansible/modules/fetch.py:12: computed key name\n"
    )


def test_plain_keys_table_shows_documented_types_and_errors() -> None:
    table = keys_table(_copy(), ROOT, use_rich=False)
    assert "checksum  ok      str  lib/ansible/modules/copy.py:5" in table
    assert "old       stale   int  -" in table
    broken = keys_table(_broken(), ROOT, use_rich=False)
    assert broken == (
        "ansible.builtin.broken: RETURN error\n"
        "error: SyntaxError: invalid syntax (line 3)\n"
    )


def test_rich_keys_table() -> None:
    pytest.importorskip("rich")
    table = keys_table(_fetch(), ROOT, use_rich=True)
    assert "dest" in table
    assert "lib/ansible/modules/fetch.py:199,208" in table
    assert "computed key name" in table


# --- markdown -----------------------------------------------------------------------


def test_audit_markdown() -> None:
    assert audit_markdown(_reports()) == (
        "| Module | RETURN | Doc | Code | Tests | Undoc | Stale |\n"
        "|---|---|--:|--:|--:|--:|--:|\n"
        "| `ansible.builtin.broken` | error | 0 | 0 | 0 | 0 | 0 |\n"
        "| `ansible.builtin.copy` | present | 2 | 1 | 0 | 0 | 1 |\n"
        "| `ansible.builtin.fetch` | missing | 0 | 1 | 2 | 2 | 0 |\n"
    )


def test_keys_markdown() -> None:
    markdown = keys_markdown(_fetch(), ROOT)
    assert markdown.startswith("## `ansible.builtin.fetch`: RETURN missing\n")
    assert "Inherits returns from: `ansible.legacy.slurp`" in markdown
    assert (
        "| `dest` | undocumented | - | `lib/ansible/modules/fetch.py:199,208` | 4 |"
        in markdown
    )
    assert "- `lib/ansible/modules/fetch.py:12`: computed key name" in markdown


def test_markdown_escapes_pipes() -> None:
    report = ModuleReport(
        module=_info("odd"),
        return_status=ReturnStatus.ERROR,
        error="bad | thing",
    )
    assert "bad \\| thing" in keys_markdown(report, ROOT)


# --- JSON ---------------------------------------------------------------------------


def _json(reports: list[ModuleReport], config: Config | None = None) -> dict[str, Any]:
    data = json.loads(reports_to_json(reports, ROOT, config or Config()))
    assert isinstance(data, dict)
    return data


def test_json_top_level() -> None:
    data = _json(_reports())
    assert data["schema_version"] == SCHEMA_VERSION == 1
    modules = data["modules"]
    assert isinstance(modules, list)
    assert [m["module"] for m in modules] == [
        "ansible.builtin.broken",
        "ansible.builtin.copy",
        "ansible.builtin.fetch",
    ]


def test_json_module_object() -> None:
    (fetch,) = [
        m for m in _json(_reports())["modules"] if m["module"].endswith("fetch")
    ]
    assert fetch["name"] == "fetch"
    assert fetch["paths"] == {
        "module": "lib/ansible/modules/fetch.py",
        "action": "lib/ansible/plugins/action/fetch.py",
        "test_target": "test/integration/targets/fetch",
    }
    assert fetch["return_status"] == "missing"
    assert fetch["error"] is None
    assert fetch["inherits_from"] == ["ansible.legacy.slurp"]
    assert fetch["counts"] == {
        "documented": 0,
        "code": 1,
        "tests": 2,
        "undocumented": 2,
        "stale": 0,
    }
    assert fetch["unresolved"] == [
        {
            "file": "lib/ansible/modules/fetch.py",
            "line": 12,
            "reason": "computed key name",
        }
    ]


def test_json_key_objects() -> None:
    (fetch,) = [
        m for m in _json(_reports())["modules"] if m["module"].endswith("fetch")
    ]
    dest, seen = fetch["keys"]
    assert dest == {
        "name": "dest",
        "status": "undocumented",
        "documented": None,
        "static": [
            {
                "file": "lib/ansible/modules/fetch.py",
                "line": 199,
                "path": "dest",
                "inferred_type": "str",
                "outcome": "success",
                "condition": None,
            },
            {
                "file": "lib/ansible/modules/fetch.py",
                "line": 208,
                "path": "dest",
                "inferred_type": None,
                "outcome": "success",
                "condition": "validate",
            },
        ],
        "observed": {
            "count": 4,
            "types": ["str"],
            "results": ["changed", "ok"],
            "sample": "/tmp/x",
            "sources": [{"file": "test/t.yml", "line": 3}],
        },
    }
    assert "sample" not in seen["observed"]  # absent, not null


def test_json_documented_key() -> None:
    (copy,) = [m for m in _json(_reports())["modules"] if m["module"].endswith("copy")]
    checksum = copy["keys"][0]
    assert checksum["documented"] == {
        "path": "checksum",
        "type": "str",
        "returned": "success",
        "has_description": True,
        "elements": None,
    }


def test_json_error_module() -> None:
    (broken,) = [
        m for m in _json(_reports())["modules"] if m["module"].endswith("broken")
    ]
    assert broken["return_status"] == "error"
    assert broken["error"] == "SyntaxError: invalid syntax (line 3)"
    assert broken["keys"] == []


def test_json_null_sample_is_kept() -> None:
    report = ModuleReport(
        module=_info("n"),
        return_status=ReturnStatus.MISSING,
        keys=(
            KeyReport(
                name="value",
                status=KeyStatus.TEST_ONLY,
                observed=Observation(path="value", count=1, sample=Sample(None)),
            ),
        ),
    )
    (module,) = _json([report])["modules"]
    observed = module["keys"][0]["observed"]
    assert "sample" in observed
    assert observed["sample"] is None


def test_json_never_contains_secrets() -> None:
    report = ModuleReport(
        module=_info("s"),
        return_status=ReturnStatus.MISSING,
        keys=(
            KeyReport(
                name="password",
                status=KeyStatus.TEST_ONLY,
                observed=Observation(
                    path="password", count=1, sample=Sample("hunter2")
                ),
            ),
            KeyReport(
                name="conn",
                status=KeyStatus.UNDOCUMENTED,
                static=(_static("conn", 1, "s", literal=Sample({"token": "abc123"})),),
                observed=Observation(
                    path="conn",
                    count=1,
                    sample=Sample({"user": "u", "token": "abc123"}),
                ),
            ),
        ),
    )
    text = reports_to_json([report], ROOT, Config())
    assert "hunter2" not in text
    assert "abc123" not in text
    (module,) = json.loads(text)["modules"]
    assert "sample" not in module["keys"][1]["observed"]  # password: dropped
    assert module["keys"][0]["observed"]["sample"] == {
        "user": "u",
        "token": "<redacted>",
    }


def test_json_is_deterministic_and_sorted() -> None:
    first = reports_to_json(_reports(), ROOT, Config())
    second = reports_to_json(list(reversed(_reports())), ROOT, Config())
    assert first == second
    assert first.endswith("\n")


def test_paths_outside_the_root_stay_absolute() -> None:
    report = ModuleReport(
        module=_info("x", module_path=Path("/elsewhere/x.py")),
        return_status=ReturnStatus.MISSING,
    )
    (module,) = _json([report])["modules"]
    assert module["paths"]["module"] == "/elsewhere/x.py"
