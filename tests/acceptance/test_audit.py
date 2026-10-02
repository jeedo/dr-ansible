"""``audit`` on the pinned ansible-core checkout: AC-1, AC-2 and AC-3 (plan task 30)."""

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from jsonschema import Draft202012Validator

from dr_ansible.cli import main

pytestmark = pytest.mark.acceptance

SCHEMA = Path(__file__).resolve().parents[2] / "schema" / "report.schema.json"

#: AC-1: modules with no RETURN at all.
MISSING = {
    "add_host",
    "assert",
    "async_wrapper",
    "blockinfile",
    "debug",
    "dnf",
    "dpkg_selections",
    "expect",
    "fail",
    "fetch",
    "group_by",
    "hostname",
    "iptables",
    "known_hosts",
    "meta",
    "package",
    "raw",
    "script",
    "set_fact",
    "set_stats",
    "setup",
}
#: AC-2: modules whose RETURN is only a placeholder.
PLACEHOLDER = {
    "assemble",
    "cron",
    "debconf",
    "lineinfile",
    "replace",
    "rpm_key",
    "service",
    "subversion",
}
#: AC-3: modules on the default exempt allowlist.
EXEMPT = {
    "gather_facts",
    "import_playbook",
    "import_role",
    "import_tasks",
    "include_role",
    "include_tasks",
}


@pytest.fixture(scope="module")
def audit(ansible_core: Path) -> tuple[int, dict[str, Any]]:
    result = CliRunner().invoke(main, ["audit", str(ansible_core), "--format", "json"])
    data = json.loads(result.stdout)
    assert isinstance(data, dict)
    return result.exit_code, data


def _with_status(data: dict[str, Any], status: str) -> set[str]:
    return {m["name"] for m in data["modules"] if m["return_status"] == status}


def test_ac1_missing(audit: tuple[int, dict[str, Any]]) -> None:
    assert _with_status(audit[1], "missing") == MISSING


def test_ac2_placeholder(audit: tuple[int, dict[str, Any]]) -> None:
    assert _with_status(audit[1], "placeholder") == PLACEHOLDER


def test_ac3_exempt(audit: tuple[int, dict[str, Any]]) -> None:
    assert _with_status(audit[1], "exempt") == EXEMPT


def test_every_other_module_documents_its_returns(
    audit: tuple[int, dict[str, Any]],
) -> None:
    statuses = {m["return_status"] for m in audit[1]["modules"]}
    assert statuses == {"missing", "placeholder", "exempt", "present"}
    assert all(m["error"] is None for m in audit[1]["modules"])


def test_findings_exit_one(audit: tuple[int, dict[str, Any]]) -> None:
    assert audit[0] == 1


def test_the_report_matches_the_schema(audit: tuple[int, dict[str, Any]]) -> None:
    Draft202012Validator(json.loads(SCHEMA.read_text())).validate(audit[1])


def test_status_filter_lists_exactly_ac1(ansible_core: Path) -> None:
    # The pre-commit use case: list only modules missing RETURN.
    result = CliRunner().invoke(
        main, ["audit", str(ansible_core), "--status", "missing", "--format", "json"]
    )
    assert result.exit_code == 1
    assert {m["name"] for m in json.loads(result.stdout)["modules"]} == MISSING


def test_without_the_allowlist_exempt_modules_are_missing(
    ansible_core: Path, tmp_path: Path
) -> None:
    config = tmp_path / "dr-ansible.toml"
    config.write_text("exempt-modules = []\n")
    result = CliRunner().invoke(
        main,
        ["audit", str(ansible_core), "--config", str(config), "--format", "json"],
    )
    data = json.loads(result.stdout)
    assert _with_status(data, "exempt") == set()
    assert _with_status(data, "missing") | _with_status(data, "placeholder") >= EXEMPT
