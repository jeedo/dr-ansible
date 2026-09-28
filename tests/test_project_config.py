"""Tests for the tool configuration in pyproject.toml and .gitignore (plan task 3)."""

import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def tool() -> dict[str, Any]:
    with (ROOT / "pyproject.toml").open("rb") as f:
        config: dict[str, Any] = tomllib.load(f)["tool"]
    return config


def test_ruff_targets_python_313_and_skips_fixtures(tool: dict[str, Any]) -> None:
    ruff = tool["ruff"]
    assert ruff["target-version"] == "py313"
    assert "tests/fixtures" in ruff["extend-exclude"]


def test_ruff_enables_rule_sets_beyond_the_default(tool: dict[str, Any]) -> None:
    selected = set(tool["ruff"]["lint"]["select"])
    assert {"E", "F", "W", "I", "B", "UP", "SIM", "RUF", "PT"} <= selected


def test_mypy_is_strict_over_src_and_tests(tool: dict[str, Any]) -> None:
    mypy = tool["mypy"]
    assert mypy["strict"] is True
    assert mypy["python_version"] == "3.13"
    assert mypy["files"] == ["src", "tests"]
    assert any("tests/fixtures" in pattern for pattern in mypy["exclude"])


def test_mypy_tolerates_untyped_ansible_only(tool: dict[str, Any]) -> None:
    overrides = tool["mypy"]["overrides"]
    untyped = [o for o in overrides if o.get("ignore_missing_imports")]
    assert untyped == [{"module": ["ansible.*"], "ignore_missing_imports": True}]


def test_pytest_registers_markers_and_is_strict(tool: dict[str, Any]) -> None:
    ini = tool["pytest"]["ini_options"]
    markers = {m.split(":", 1)[0] for m in ini["markers"]}
    assert markers == {"acceptance", "runtime"}
    assert "--strict-markers" in ini["addopts"]
    assert "--strict-config" in ini["addopts"]
    assert ini["testpaths"] == ["tests"]
    assert ini["xfail_strict"] is True


def test_pytest_deselects_slow_suites_by_default(tool: dict[str, Any]) -> None:
    addopts = tool["pytest"]["ini_options"]["addopts"]
    assert "-m" in addopts
    assert addopts[addopts.index("-m") + 1] == "not acceptance and not runtime"


def _ignored(path: str) -> bool:
    result = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={ROOT}",
            "check-ignore",
            "-q",
            "--no-index",
            path,
        ],
        cwd=ROOT,
        check=False,
    )
    return result.returncode == 0


@pytest.mark.parametrize(
    "path",
    [
        "tests/fixtures/core/lib/ansible/modules/ping.py",
        "tests/fixtures/core/lib/ansible/plugins/action/fetch.py",
    ],
)
def test_fixture_lib_trees_are_not_gitignored(path: str) -> None:
    assert not _ignored(path)


def test_top_level_lib_is_still_gitignored() -> None:
    assert _ignored("lib/something.py")
