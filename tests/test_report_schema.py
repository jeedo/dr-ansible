"""The published JSON report schema (plan task 22: FR-21)."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from dr_ansible.config import Config
from dr_ansible.discovery import detect_project, discover_modules
from dr_ansible.docs_audit import audit_docs
from dr_ansible.mining.static_miner import mine_target
from dr_ansible.model import (
    KeyReport,
    KeyStatus,
    Language,
    ModuleReport,
    Observation,
    Outcome,
    ResultState,
    ReturnStatus,
    ReturnType,
    Sample,
)
from dr_ansible.reconcile import build_report
from dr_ansible.report import SCHEMA_VERSION, reports_to_json
from dr_ansible.static.action_analyzer import analyze_action
from dr_ansible.static.module_analyzer import analyze_module
from tests.test_report import ROOT, _info, _reports

REPO = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO / "schema" / "report.schema.json"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    data = json.loads(SCHEMA_PATH.read_text())
    assert isinstance(data, dict)
    return data


@pytest.fixture(scope="module")
def validator(schema: dict[str, Any]) -> Draft202012Validator:
    return Draft202012Validator(schema)


def _document(reports: list[ModuleReport]) -> dict[str, Any]:
    data = json.loads(reports_to_json(reports, ROOT, Config()))
    assert isinstance(data, dict)
    return data


# --- the schema itself ----------------------------------------------------------------


def test_schema_is_a_valid_draft_2020_12_schema(schema: dict[str, Any]) -> None:
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["title"]
    assert schema["$id"].endswith("/schema/report.schema.json")


def test_schema_version_matches_the_code(schema: dict[str, Any]) -> None:
    assert schema["properties"]["schema_version"] == {"const": SCHEMA_VERSION}


@pytest.mark.parametrize(
    ("definition", "enum"),
    [
        ("return_status", ReturnStatus),
        ("key_status", KeyStatus),
        ("return_type", ReturnType),
        ("outcome", Outcome),
        ("result_state", ResultState),
    ],
)
def test_schema_enums_match_the_model(
    schema: dict[str, Any], definition: str, enum: type[Any]
) -> None:
    assert schema["$defs"][definition]["enum"] == [m.value for m in enum]


# --- output validates ---------------------------------------------------------------


def test_synthetic_reports_validate(validator: Draft202012Validator) -> None:
    validator.validate(_document(_reports()))


def test_empty_report_validates(validator: Draft202012Validator) -> None:
    validator.validate(_document([]))


def test_edge_cases_validate(validator: Draft202012Validator) -> None:
    reports = [
        ModuleReport(
            module=_info("nulls", module_path=Path("/elsewhere/nulls.py")),
            return_status=ReturnStatus.MISSING,
            keys=(
                KeyReport(
                    name="value",
                    status=KeyStatus.TEST_ONLY,
                    observed=Observation(path="value", count=1, sample=Sample(None)),
                ),
                KeyReport(
                    name="value.nested",
                    status=KeyStatus.TEST_ONLY,
                    observed=Observation(
                        path="value.nested",
                        count=2,
                        sample=Sample({"list": [1, 2.5, True], "token": "x"}),
                    ),
                ),
            ),
        ),
        ModuleReport(
            module=_info("winmod", language=Language.POWERSHELL),
            return_status=ReturnStatus.UNSUPPORTED,
        ),
    ]
    validator.validate(_document(reports))


def _fixture_reports(root: Path) -> list[ModuleReport]:
    project = detect_project(root)
    config = Config()
    reports = []
    for module in discover_modules(project):
        if module.language is Language.POWERSHELL:
            continue
        docs = audit_docs(module, config)
        analysis = analyze_module(module.module_path)
        static = list(analysis.keys)
        unresolved = list(analysis.unresolved)
        inherits: list[str] = []
        if module.action_path is not None:
            action = analyze_action(module.action_path, module.fqcn)
            static += action.keys
            unresolved += action.unresolved
            inherits += action.inherits_from
        reports.append(
            build_report(
                module=module,
                docs=docs,
                static=static,
                unresolved=unresolved,
                inherits_from=inherits,
                observations=mine_target(module).observations,
                config=config,
            )
        )
    return reports


@pytest.mark.parametrize("fixture", ["core", "collection", "built_collection"])
def test_fixture_projects_validate(
    validator: Draft202012Validator, fixture: str
) -> None:
    root = FIXTURES / fixture
    reports = _fixture_reports(root)
    assert reports
    data = json.loads(reports_to_json(reports, root, Config()))
    validator.validate(data)


# --- the schema rejects what the code never writes ----------------------------------


def _fetch(data: dict[str, Any]) -> dict[str, Any]:
    (module,) = [m for m in data["modules"] if m["name"] == "fetch"]
    assert isinstance(module, dict)
    return module


def _rejects(validator: Draft202012Validator, data: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        validator.validate(data)


def test_rejects_another_schema_version(validator: Draft202012Validator) -> None:
    data = _document(_reports())
    data["schema_version"] = SCHEMA_VERSION + 1
    _rejects(validator, data)


@pytest.mark.parametrize(
    "field",
    [
        "module",
        "name",
        "paths",
        "return_status",
        "error",
        "inherits_from",
        "counts",
        "unresolved",
        "keys",
    ],
)
def test_rejects_a_module_without_a_required_field(
    validator: Draft202012Validator, field: str
) -> None:
    data = _document(_reports())
    del _fetch(data)[field]
    _rejects(validator, data)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("return_status",), "fine"),
        (("paths", "elsewhere"), "x.py"),
        (("counts", "stale"), -1),
        (("unresolved", 0, "line"), 0),
        (("keys", 0, "status"), "maybe"),
        (("keys", 0, "static", 0, "outcome"), "maybe"),
        (("keys", 0, "static", 0, "inferred_type"), "string"),
        (("keys", 0, "observed", "count"), 0),
        (("keys", 0, "observed", "results"), ["skipped"]),
        (("keys", 0, "extra"), True),
    ],
)
def test_rejects_bad_values(
    validator: Draft202012Validator, path: tuple[str | int, ...], value: Any
) -> None:
    data = _document(_reports())
    target: Any = _fetch(data)
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = value
    _rejects(validator, data)


def test_sample_is_optional_and_sources_need_lines(
    validator: Draft202012Validator,
) -> None:
    data = _document(_reports())
    observed = _fetch(data)["keys"][0]["observed"]
    without = copy.deepcopy(data)
    del _fetch(without)["keys"][0]["observed"]["sample"]
    validator.validate(without)
    observed["sources"] = [{"file": "t.yml"}]  # a source needs a line
    _rejects(validator, data)
