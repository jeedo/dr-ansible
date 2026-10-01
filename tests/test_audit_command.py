"""Tests for the ``audit`` command and the per-module pipeline (plan task 23)."""

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result
from jsonschema import Draft202012Validator

from dr_ansible import pipeline
from dr_ansible.cli import main
from dr_ansible.config import Config
from dr_ansible.discovery import detect_project, discover_modules
from dr_ansible.model import KeyStatus, ReturnStatus
from dr_ansible.pipeline import analyze, has_findings

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CORE = FIXTURES / "core"
COLLECTION = FIXTURES / "collection"
SCHEMA = Path(__file__).resolve().parent.parent / "schema" / "report.schema.json"


def _audit(*args: str | Path) -> Result:
    return CliRunner().invoke(main, ["audit", *map(str, args)])


def _json(*args: str | Path) -> tuple[int, dict[str, Any]]:
    result = _audit(*args, "--format", "json")
    data = json.loads(result.stdout)
    assert isinstance(data, dict)
    return result.exit_code, data


def _statuses(data: dict[str, Any]) -> dict[str, str]:
    return {m["name"]: m["return_status"] for m in data["modules"]}


def _collection(tmp_path: Path, modules: dict[str, str]) -> Path:
    (tmp_path / "galaxy.yml").write_text("namespace: ns\nname: coll\nversion: 1.0.0\n")
    plugins = tmp_path / "plugins" / "modules"
    plugins.mkdir(parents=True)
    for name, source in modules.items():
        (plugins / f"{name}.py").write_text(source)
    return tmp_path


DOCUMENTED = (
    'RETURN = r"""\nname:\n    description: The name.\n'
    '    returned: always\n    type: str\n"""\n'
    "def main():\n    m.exit_json(name='x')\n"
)
UNDOCUMENTED = "def main():\n    m.exit_json(name='x')\n"
BROKEN = "def main(:\n    pass\n"


# --- the pipeline -------------------------------------------------------------------


def _module(name: str, root: Path = CORE) -> Any:
    return {m.name: m for m in discover_modules(detect_project(root))}[name]


def test_analyze_builds_the_full_report() -> None:
    result = analyze(_module("helper"), Config())
    assert result.report.return_status is ReturnStatus.PRESENT
    assert {k.name: k.status for k in result.report.keys} == {
        "legacy_id": KeyStatus.STALE,
        "name": KeyStatus.OK,
        "owner": KeyStatus.UNDOCUMENTED,
        "state": KeyStatus.OK,
    }
    assert result.docs is not None
    assert result.docs.status is ReturnStatus.PRESENT


def test_analyze_includes_action_plugins_and_tests() -> None:
    report = analyze(_module("hybrid"), Config()).report
    assert report.inherits_from == ("ansible.builtin.hybrid",)
    keywords = {k.name: k for k in analyze(_module("keywords"), Config()).report.keys}
    assert keywords["rc"].observed is not None


def test_analyze_include_common() -> None:
    plain = {k.name for k in analyze(_module("virtual"), Config()).report.keys}
    common = analyze(_module("virtual"), Config(), include_common=True).report.keys
    assert "changed" not in plain
    assert "changed" in {k.name for k in common}


def test_powershell_modules_are_unsupported() -> None:
    result = analyze(_module("win_ping"), Config())
    assert result.report.return_status is ReturnStatus.UNSUPPORTED
    assert result.report.keys == ()
    assert result.docs is None


def test_a_module_that_cannot_be_parsed_is_an_error(tmp_path: Path) -> None:
    root = _collection(tmp_path, {"broken": BROKEN})
    result = analyze(_module("broken", root), Config())
    assert result.report.return_status is ReturnStatus.ERROR
    assert result.report.error is not None
    assert "broken.py" in result.report.error
    assert result.report.keys == ()
    assert result.docs is None


def test_an_unexpected_failure_is_an_error_too(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(path: Path) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(pipeline, "analyze_module", explode)
    report = analyze(_module("helper"), Config()).report
    assert report.return_status is ReturnStatus.ERROR
    assert report.error == "internal error: RuntimeError: boom"


@pytest.mark.parametrize(
    ("name", "finding"),
    [
        ("incremental", True),  # missing
        ("keywords", True),  # placeholder
        ("invalid", True),  # invalid
        ("helper", True),  # present, with undocumented and stale keys
        ("nested", False),  # present and in step with the code
        ("include_tasks", False),  # exempt
        ("win_ping", False),  # unsupported
    ],
)
def test_has_findings(name: str, finding: bool) -> None:
    assert has_findings(analyze(_module(name), Config()).report) is finding


def test_errors_are_findings(tmp_path: Path) -> None:
    root = _collection(tmp_path, {"broken": BROKEN})
    assert has_findings(analyze(_module("broken", root), Config()).report)


# --- audit: output ------------------------------------------------------------------


def test_audit_lists_every_module() -> None:
    code, data = _json(CORE)
    assert code == 1
    assert _statuses(data) == {
        "dynamic": "missing",
        "helper": "present",
        "hybrid": "present",
        "include_tasks": "exempt",
        "incremental": "missing",
        "invalid": "invalid",
        "keywords": "placeholder",
        "nested": "present",
        "raises_on_import": "missing",
        "sidecar": "present",
        "virtual": "missing",
        "win_ping": "unsupported",
    }


def test_audit_json_matches_the_schema() -> None:
    _, data = _json(CORE)
    Draft202012Validator(json.loads(SCHEMA.read_text())).validate(data)
    (helper,) = [m for m in data["modules"] if m["name"] == "helper"]
    assert helper["paths"]["module"] == "lib/ansible/modules/helper.py"


def test_audit_table_is_the_default() -> None:
    result = _audit(CORE, "--module", "nested")
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert lines[0].split() == [
        "MODULE",
        "RETURN",
        "DOC",
        "CODE",
        "TESTS",
        "UNDOC",
        "STALE",
    ]
    assert lines[1].split() == [
        "ansible.builtin.nested",
        "present",
        "4",
        "4",
        "0",
        "0",
        "0",
    ]


def test_audit_markdown() -> None:
    result = _audit(CORE, "--module", "nested", "--format", "markdown")
    assert result.stdout == (
        "| Module | RETURN | Doc | Code | Tests | Undoc | Stale |\n"
        "|---|---|--:|--:|--:|--:|--:|\n"
        "| `ansible.builtin.nested` | present | 4 | 4 | 0 | 0 | 0 |\n"
    )


def test_audit_of_a_collection() -> None:
    code, data = _json(COLLECTION)
    assert code == 1  # widget's `name` is undocumented
    assert [m["module"] for m in data["modules"]] == ["example.widgets.widget"]


def test_audit_output_is_deterministic() -> None:
    assert (
        _audit(CORE, "--format", "json").stdout
        == _audit(CORE, "--format", "json").stdout
    )


# --- audit: filters -----------------------------------------------------------------


def test_module_filter_takes_names_aliases_and_globs() -> None:
    _, data = _json(CORE, "--module", "nest*", "-m", "ansible.builtin.old_incremental")
    assert set(_statuses(data)) == {"nested", "incremental"}


def test_module_filter_takes_a_comma_separated_list() -> None:
    _, data = _json(CORE, "--module", "nested,helper")
    assert set(_statuses(data)) == {"nested", "helper"}


def test_status_filter() -> None:
    code, data = _json(CORE, "--status", "missing,placeholder")
    assert code == 1
    assert set(_statuses(data).values()) == {"missing", "placeholder"}
    assert set(_statuses(data)) == {
        "dynamic",
        "incremental",
        "keywords",
        "raises_on_import",
        "virtual",
    }


def test_status_filter_can_be_repeated() -> None:
    _, data = _json(CORE, "--status", "exempt", "--status", "unsupported")
    assert _statuses(data) == {"include_tasks": "exempt", "win_ping": "unsupported"}


def test_filters_combine() -> None:
    _, data = _json(CORE, "--module", "*i*", "--status", "missing,exempt")
    assert set(_statuses(data)) == {
        "dynamic",
        "include_tasks",
        "incremental",
        "raises_on_import",
        "virtual",
    }


# --- audit: exit codes --------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "code"),
    [
        (["--module", "nested"], 0),
        (["--module", "include_tasks"], 0),
        (["--module", "win_ping"], 0),
        (["--module", "helper"], 1),
        (["--status", "missing"], 1),
        (["--status", "present", "--module", "nested,sidecar"], 0),
        (["--module", "no_such_glob*"], 0),  # nothing selected, nothing missing
    ],
)
def test_exit_codes_for_findings(args: list[str], code: int) -> None:
    assert _audit(CORE, *args).exit_code == code


def test_a_clean_collection_exits_zero(tmp_path: Path) -> None:
    root = _collection(tmp_path, {"good": DOCUMENTED})
    result = _audit(root)
    assert result.exit_code == 0, result.output


def test_an_undocumented_collection_exits_one(tmp_path: Path) -> None:
    root = _collection(tmp_path, {"good": DOCUMENTED, "bad": UNDOCUMENTED})
    assert _audit(root).exit_code == 1


def test_one_broken_module_does_not_stop_the_run(tmp_path: Path) -> None:
    root = _collection(tmp_path, {"good": DOCUMENTED, "broken": BROKEN})
    result = _audit(root, "--format", "json")
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert _statuses(data) == {"broken": "error", "good": "present"}
    (broken,) = [m for m in data["modules"] if m["name"] == "broken"]
    assert "broken.py" in broken["error"]


def test_unknown_module_name_is_a_usage_error() -> None:
    result = _audit(CORE, "--module", "no_such_module")
    assert result.exit_code == 2
    assert "no module named 'no_such_module'" in result.stderr
    assert result.stdout == ""


def test_unknown_status_is_a_usage_error() -> None:
    result = _audit(CORE, "--status", "missing,bogus")
    assert result.exit_code == 2
    assert "bogus" in result.stderr


def test_unknown_format_is_a_usage_error() -> None:
    assert _audit(CORE, "--format", "yaml").exit_code == 2


def test_missing_path_is_a_usage_error(tmp_path: Path) -> None:
    assert _audit(tmp_path / "nowhere").exit_code == 2


def test_a_directory_that_is_not_a_project_is_an_error(tmp_path: Path) -> None:
    result = _audit(tmp_path)
    assert result.exit_code == 2
    assert str(tmp_path) in result.stderr


def test_bad_config_is_an_error(tmp_path: Path) -> None:
    root = _collection(tmp_path, {"good": DOCUMENTED})
    (root / "dr-ansible.toml").write_text("nonsense = 1\n")
    result = _audit(root)
    assert result.exit_code == 2
    assert "unknown setting 'nonsense'" in result.stderr


def test_explicit_config_file(tmp_path: Path) -> None:
    root = _collection(tmp_path, {"bad": UNDOCUMENTED})
    config = tmp_path / "custom.toml"
    config.write_text('exempt-modules = ["bad"]\n')
    _, data = _json(root, "--config", config)
    assert _statuses(data) == {"bad": "exempt"}
    assert _audit(root, "--config", tmp_path / "absent.toml").exit_code == 2
