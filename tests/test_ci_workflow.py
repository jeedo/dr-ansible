"""Tests for the CI workflow (plan task 4)."""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
SHA_PIN = re.compile(r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")


@pytest.fixture(scope="module")
def workflow() -> dict[Any, Any]:
    data: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text())
    return data


@pytest.fixture(scope="module")
def steps(workflow: dict[Any, Any]) -> list[dict[str, Any]]:
    (job,) = workflow["jobs"].values()
    result: list[dict[str, Any]] = job["steps"]
    return result


def _runs(steps: list[dict[str, Any]]) -> list[str]:
    return [s["run"].strip() for s in steps if "run" in s]


def test_runs_on_push_and_pull_request_to_main(workflow: dict[Any, Any]) -> None:
    # PyYAML (YAML 1.1) parses the bare key `on` as boolean True.
    triggers = workflow[True]
    assert triggers["push"]["branches"] == ["main"]
    assert triggers["pull_request"]["branches"] == ["main"]


def test_token_is_read_only(workflow: dict[Any, Any]) -> None:
    assert workflow["permissions"] == {"contents": "read"}


def test_third_party_actions_are_pinned_to_commit_shas(
    steps: list[dict[str, Any]],
) -> None:
    uses = [s["uses"] for s in steps if "uses" in s]
    assert uses
    for action in uses:
        assert SHA_PIN.match(action), action


def test_python_comes_from_python_version_file(steps: list[dict[str, Any]]) -> None:
    assert (ROOT / ".python-version").read_text().strip() == "3.13"
    installs = [r for r in _runs(steps) if r.startswith("uv python install")]
    # No explicit version: uv reads .python-version, so CI and local dev agree.
    assert installs == ["uv python install"]
    assert not any("3.11" in r for r in _runs(steps))


def test_dependencies_install_from_the_lock_file(steps: list[dict[str, Any]]) -> None:
    assert "uv sync --locked --group dev" in _runs(steps)


@pytest.mark.parametrize(
    "command",
    [
        "uv run ruff check .",
        "uv run ruff format --check .",
        "uv run mypy",
        "uv run pytest",
        "uv run python scripts/check_docs.py",
    ],
)
def test_runs_each_check(steps: list[dict[str, Any]], command: str) -> None:
    assert command in _runs(steps)


def test_checks_run_after_dependencies_are_installed(
    steps: list[dict[str, Any]],
) -> None:
    runs = _runs(steps)
    sync = runs.index("uv sync --locked --group dev")
    assert all(runs.index(c) > sync for c in runs if c.startswith("uv run"))
