"""The pinned checkout itself: the ground every acceptance test stands on."""

import subprocess
from pathlib import Path

import pytest

from dr_ansible.discovery import Layout, detect_project, discover_modules
from tests.acceptance.checkout import pinned

pytestmark = pytest.mark.acceptance


def test_checkout_is_at_the_pinned_commit(ansible_core: Path) -> None:
    head = subprocess.run(
        ["git", "-C", str(ansible_core), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head == pinned().commit


def test_checkout_is_an_ansible_core_project(ansible_core: Path) -> None:
    project = detect_project(ansible_core)
    assert project.layout is Layout.CORE
    names = {m.name for m in discover_modules(project)}
    assert {"fetch", "copy", "stat", "ping", "setup"} <= names
    assert len(names) > 60
