"""Tests for the ``returns`` command (plan task 24)."""

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result
from jsonschema import Draft202012Validator

from dr_ansible.cli import main

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CORE = FIXTURES / "core"
COLLECTION = FIXTURES / "collection"
SCHEMA = Path(__file__).resolve().parent.parent / "schema" / "report.schema.json"


def _returns(*args: str | Path) -> Result:
    return CliRunner().invoke(main, ["returns", *map(str, args)])


def _json(*args: str | Path) -> dict[str, Any]:
    result = _returns(*args, "--format", "json")
    data = json.loads(result.stdout)
    assert isinstance(data, dict)
    return data


def _keys(data: dict[str, Any]) -> dict[str, str]:
    (module,) = data["modules"]
    return {k["name"]: k["status"] for k in module["keys"]}


# --- output -------------------------------------------------------------------------


def test_table_shows_keys_with_status_and_evidence() -> None:
    result = _returns(CORE, "helper")
    assert result.exit_code == 1  # owner is undocumented, legacy_id is stale
    lines = result.stdout.splitlines()
    assert lines[0] == "ansible.builtin.helper: RETURN present"
    rows = {line.split()[0]: line.split()[1:] for line in lines[2:] if line}
    assert rows["KEY"] == ["STATUS", "DOC", "CODE", "TESTS"]
    assert rows["owner"][:2] == ["undocumented", "-"]
    assert rows["owner"][2].startswith("lib/ansible/modules/helper.py:")
    assert rows["legacy_id"][:2] == ["stale", "int"]
    assert rows["name"][0] == "ok"


def test_table_shows_inheritance_and_unresolved_keys() -> None:
    hybrid = _returns(CORE, "hybrid").stdout
    assert "inherits returns from: ansible.builtin.hybrid" in hybrid
    dynamic = _returns(CORE, "dynamic").stdout
    assert "unresolved:\n  lib/ansible/modules/dynamic.py:" in dynamic
    assert "computed key name" in dynamic


def test_json_is_one_module_and_matches_the_schema() -> None:
    data = _json(CORE, "keywords")
    Draft202012Validator(json.loads(SCHEMA.read_text())).validate(data)
    (module,) = data["modules"]
    assert module["module"] == "ansible.builtin.keywords"
    assert module["return_status"] == "placeholder"
    rc = {k["name"]: k for k in module["keys"]}["rc"]
    assert [s["outcome"] for s in rc["static"]] == ["failure"]
    assert rc["observed"]["count"] >= 1


def test_markdown() -> None:
    result = _returns(CORE, "helper", "--format", "markdown")
    assert result.stdout.startswith("## `ansible.builtin.helper`: RETURN present\n")
    assert "| `owner` | undocumented | - |" in result.stdout


def test_common_keys_are_hidden_unless_asked_for() -> None:
    plain = _keys(_json(CORE, "virtual"))
    common = _keys(_json(CORE, "virtual", "--include-common"))
    assert "changed" not in plain
    assert common["changed"] == "undocumented"
    assert set(plain) < set(common)


def test_collection_module() -> None:
    assert _keys(_json(COLLECTION, "example.widgets.widget")) == {
        "name": "undocumented",
        "widget_id": "ok",
    }


# --- choosing the module ------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "incremental",
        "ansible.builtin.incremental",
        "ansible.legacy.incremental",
        "old_incremental",  # a redirect
        "increment*",  # a glob matching one module
    ],
)
def test_module_by_any_name(name: str) -> None:
    (module,) = _json(CORE, name)["modules"]
    assert module["module"] == "ansible.builtin.incremental"


def test_unknown_module_is_a_usage_error() -> None:
    result = _returns(CORE, "no_such_module")
    assert result.exit_code == 2
    assert "no module named 'no_such_module'" in result.stderr
    assert result.stdout == ""


def test_glob_matching_nothing_is_a_usage_error() -> None:
    result = _returns(CORE, "zz*")
    assert result.exit_code == 2
    assert "no module matches 'zz*'" in result.stderr


def test_glob_matching_several_modules_is_a_usage_error() -> None:
    result = _returns(CORE, "h*")
    assert result.exit_code == 2
    assert (
        "'h*' matches 2 modules (ansible.builtin.helper, ansible.builtin.hybrid)"
        in result.stderr
    )


def test_module_argument_is_required() -> None:
    assert _returns(CORE).exit_code == 2


# --- exit codes ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("nested", 0),  # documented and in step with the code
        ("sidecar", 0),
        ("include_tasks", 0),  # exempt
        ("win_ping", 0),  # unsupported
        ("helper", 1),
        ("incremental", 1),  # missing
        ("invalid", 1),
    ],
)
def test_exit_codes(name: str, code: int) -> None:
    assert _returns(CORE, name).exit_code == code


def test_unsupported_module_says_so() -> None:
    assert _returns(CORE, "win_ping").stdout == (
        "ansible.builtin.win_ping: RETURN unsupported\n\nno return keys found\n"
    )


def test_a_broken_module_reports_its_error(tmp_path: Path) -> None:
    (tmp_path / "galaxy.yml").write_text("namespace: ns\nname: coll\nversion: 1.0.0\n")
    modules = tmp_path / "plugins" / "modules"
    modules.mkdir(parents=True)
    (modules / "broken.py").write_text("def main(:\n")
    result = _returns(tmp_path, "broken")
    assert result.exit_code == 1
    assert result.stdout.startswith("ns.coll.broken: RETURN error\nerror: ")
    assert "broken.py" in result.stdout


def test_a_directory_that_is_not_a_project_is_an_error(tmp_path: Path) -> None:
    result = _returns(tmp_path, "anything")
    assert result.exit_code == 2
    assert "not an ansible-core checkout" in result.stderr
