"""Dependabot version updates, as in jeedo/oneshot (plan task 36)."""

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
DEPENDABOT = ROOT / ".github" / "dependabot.yml"
WORKFLOWS = ROOT / ".github" / "workflows"
SETTINGS = ROOT / "docs" / "repository-settings.md"


@pytest.fixture(scope="module")
def config() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(DEPENDABOT.read_text())
    return data


def _updates(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {u["package-ecosystem"]: u for u in config["updates"]}


def test_version_2(config: dict[str, Any]) -> None:
    assert config["version"] == 2


def test_uv_and_github_actions_are_updated(config: dict[str, Any]) -> None:
    # uv covers pyproject.toml and uv.lock; github-actions the SHA-pinned actions.
    assert set(_updates(config)) == {"uv", "github-actions"}
    for update in config["updates"]:
        assert update["directory"] == "/"


def test_updates_are_weekly(config: dict[str, Any]) -> None:
    for update in config["updates"]:
        assert update["schedule"]["interval"] == "weekly"


def test_minor_and_patch_are_grouped_but_major_stays_separate(
    config: dict[str, Any],
) -> None:
    for ecosystem, update in _updates(config).items():
        (group,) = update["groups"].values()
        # "major" is absent, so a breaking bump always gets its own PR.
        assert group["update-types"] == ["minor", "patch"], ecosystem


def test_no_workflow_auto_merges_dependabot_prs() -> None:
    for workflow in WORKFLOWS.glob("*.yml"):
        text = workflow.read_text()
        assert "--auto" not in text, workflow.name
        assert "automerge" not in text.replace("-", "").lower(), workflow.name


def test_ci_runs_on_dependabot_prs() -> None:
    # Dependabot opens pull requests against main, which ci.yml always checks.
    ci = yaml.safe_load((WORKFLOWS / "ci.yml").read_text())
    assert ci[True]["pull_request"]["branches"] == ["main"]
    assert set(ci["jobs"]) >= {"test", "acceptance"}


def test_manual_repository_settings_are_documented() -> None:
    # Security updates and branch protection are owner settings, not files.
    text = SETTINGS.read_text()
    for needle in (
        "Dependabot security updates",
        "Require a pull request before merging",
        "Require status checks to pass",
        "`test`",
        "`acceptance`",
        "auto-merge",
    ):
        assert needle in text, needle
