"""Tests for the declared dependencies (plan task 2, NFR-3, NFR-4)."""

import importlib
from importlib.metadata import metadata, requires

import pytest
from packaging.requirements import Requirement

RUNTIME = {"ansible-core", "click", "pyyaml"}
OPTIONAL = {"rich": "rich"}
# NFR-4: every runtime dependency must be GPLv3-compatible.
GPL3_COMPATIBLE = {"GPL-3.0-or-later", "MIT", "BSD-3-Clause", "Apache-2.0"}


def _requirements() -> list[Requirement]:
    return [Requirement(r) for r in requires("dr-ansible") or []]


def test_runtime_dependencies_are_exactly_the_minimal_set() -> None:
    names = {r.name.lower() for r in _requirements() if r.marker is None}
    assert names == RUNTIME


def test_ansible_core_is_at_least_latest_release_at_design_time() -> None:
    (req,) = [r for r in _requirements() if r.name == "ansible-core"]
    assert req.specifier.contains("2.21.4")
    assert not req.specifier.contains("2.21.3")


@pytest.mark.parametrize(("extra", "package"), OPTIONAL.items())
def test_rich_is_an_optional_extra(extra: str, package: str) -> None:
    extras = [r for r in _requirements() if r.marker is not None]
    assert any(
        r.name == package
        and r.marker is not None
        and r.marker.evaluate({"extra": extra})
        for r in extras
    )
    assert extra in (metadata("dr-ansible").get_all("Provides-Extra") or [])


def test_ansible_doc_parser_is_importable() -> None:
    plugin_docs = importlib.import_module("ansible.parsing.plugin_docs")
    assert callable(plugin_docs.read_docstring)


def test_yaml_is_importable() -> None:
    yaml = importlib.import_module("yaml")
    assert yaml.safe_load("a: 1") == {"a": 1}


@pytest.mark.parametrize("dist", sorted(RUNTIME))
def test_runtime_dependency_license_is_gpl3_compatible(dist: str) -> None:
    meta = metadata(dist)
    license_expr = meta.get("License-Expression")
    classifiers = meta.get_all("Classifier") or []
    if license_expr is not None:
        assert license_expr in GPL3_COMPATIBLE
    else:
        assert any(
            c.startswith("License :: OSI Approved :: ")
            and c.rsplit(" :: ", 1)[1].split(" License")[0] in {"MIT", "BSD"}
            for c in classifiers
        ), classifiers


@pytest.mark.parametrize("dist", ["ruff", "mypy", "jsonschema", "types-PyYAML"])
def test_dev_dependency_is_installed_in_project_env(dist: str) -> None:
    # Checks distribution metadata, not PATH, so a globally installed tool does not count.
    assert metadata(dist)["Name"].lower() == dist.lower()
