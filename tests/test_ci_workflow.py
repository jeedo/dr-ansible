"""Tests for the CI workflows (plan tasks 4 and 35)."""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
RUNTIME_WORKFLOW = ROOT / ".github" / "workflows" / "runtime.yml"
SHA_PIN = re.compile(r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")


@pytest.fixture(scope="module")
def workflow() -> dict[Any, Any]:
    data: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text())
    return data


@pytest.fixture(scope="module")
def steps(workflow: dict[Any, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = workflow["jobs"]["test"]["steps"]
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


# --- acceptance tests in CI (plan task 35) ------------------------------------------


def _load(path: Path) -> dict[Any, Any]:
    data: dict[Any, Any] = yaml.safe_load(path.read_text())
    return data


def _pinned(steps: list[dict[str, Any]]) -> None:
    uses = [s["uses"] for s in steps if "uses" in s]
    assert uses
    for action in uses:
        assert SHA_PIN.match(action), action


def _cache_step(steps: list[dict[str, Any]]) -> dict[str, Any]:
    (cache,) = [s for s in steps if s.get("uses", "").startswith("actions/cache@")]
    return cache


@pytest.fixture(scope="module")
def acceptance(workflow: dict[Any, Any]) -> dict[str, Any]:
    job: dict[str, Any] = workflow["jobs"]["acceptance"]
    return job


def test_acceptance_job_runs_after_the_unit_tests(acceptance: dict[str, Any]) -> None:
    assert acceptance["needs"] == "test"
    _pinned(acceptance["steps"])


def test_acceptance_job_caches_the_pinned_checkout(acceptance: dict[str, Any]) -> None:
    steps = acceptance["steps"]
    cache = _cache_step(steps)
    assert cache["with"]["path"] == "~/.cache/dr-ansible"
    # The key changes whenever the pin (in checkout.py) changes.
    assert "hashFiles('tests/acceptance/checkout.py')" in cache["with"]["key"]
    (fetch,) = [
        s
        for s in steps
        if s.get("run", "").strip() == "uv run python -m tests.acceptance.checkout"
    ]
    assert steps.index(cache) < steps.index(fetch)


def test_acceptance_job_runs_acceptance_but_not_runtime(
    acceptance: dict[str, Any],
) -> None:
    runs = _runs(acceptance["steps"])
    assert "uv sync --locked --group dev" in runs
    assert 'uv run pytest -m "acceptance and not runtime"' in runs


# --- runtime tests (plan task 35) ---------------------------------------------------


@pytest.fixture(scope="module")
def runtime() -> dict[Any, Any]:
    return _load(RUNTIME_WORKFLOW)


def test_runtime_runs_on_demand_and_weekly(runtime: dict[Any, Any]) -> None:
    triggers = runtime[True]
    assert set(triggers) == {"workflow_dispatch", "schedule"}
    (schedule,) = triggers["schedule"]
    _minute, _hour, day, month, weekday = schedule["cron"].split()
    assert (day, month) == ("*", "*")
    assert weekday.isdigit()  # once a week


def test_runtime_token_is_read_only(runtime: dict[Any, Any]) -> None:
    assert runtime["permissions"] == {"contents": "read"}


def test_runtime_job_requires_docker_and_runs_runtime_tests(
    runtime: dict[Any, Any],
) -> None:
    (job,) = runtime["jobs"].values()
    steps = job["steps"]
    _pinned(steps)
    _cache_step(steps)
    run = [
        s for s in steps if s.get("run", "").strip() == "uv run pytest -m runtime -rs"
    ]
    assert len(run) == 1
    # Docker is there on GitHub's runners: its tests must not be skipped.
    assert run[0]["env"]["DR_ANSIBLE_REQUIRE_DOCKER"] == "1"
