"""Tests for name routing, aliases and --module filtering (plan task 9).

Covers FR-3 (short names, ansible.builtin.*, ansible.legacy.* and collection
FQCNs map to one module), FR-4 (--module names and globs) and NFR-6
(deterministic order).
"""

import re
from pathlib import Path

import pytest

from dr_ansible.discovery import (
    DiscoveryError,
    Project,
    detect_project,
    discover_modules,
    filter_modules,
    load_redirects,
)
from dr_ansible.model import ModuleInfo

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CORE = FIXTURES / "core"
COLLECTION = FIXTURES / "collection"
BUILT = FIXTURES / "built_collection"


def _modules(root: Path) -> dict[str, ModuleInfo]:
    return {m.name: m for m in discover_modules(detect_project(root))}


def _collection(tmp_path: Path, *modules: str, runtime: str | None = None) -> Path:
    (tmp_path / "galaxy.yml").write_text("namespace: ns\nname: coll\n")
    modules_dir = tmp_path / "plugins" / "modules"
    modules_dir.mkdir(parents=True)
    for name in modules:
        (modules_dir / f"{name}.py").write_text("")
    if runtime is not None:
        (tmp_path / "meta").mkdir()
        (tmp_path / "meta" / "runtime.yml").write_text(runtime)
    return tmp_path


def _core(tmp_path: Path, *modules: str, runtime: str | None = None) -> Path:
    modules_dir = tmp_path / "lib" / "ansible" / "modules"
    modules_dir.mkdir(parents=True)
    for name in modules:
        (modules_dir / f"{name}.py").write_text("")
    if runtime is not None:
        config = tmp_path / "lib" / "ansible" / "config"
        config.mkdir()
        (config / "ansible_builtin_runtime.yml").write_text(runtime)
    return tmp_path


# --- routing file ------------------------------------------------------------------


def test_project_knows_its_routing_file() -> None:
    core = detect_project(CORE)
    assert core.routing_file == CORE / "lib/ansible/config/ansible_builtin_runtime.yml"
    assert detect_project(COLLECTION).routing_file == COLLECTION / "meta/runtime.yml"


def test_load_redirects_from_core_fixture() -> None:
    redirects = load_redirects(detect_project(CORE))
    assert redirects == {"old_incremental": "ansible.builtin.incremental"}


def test_load_redirects_from_collection_fixture() -> None:
    redirects = load_redirects(detect_project(COLLECTION))
    assert redirects == {"old_widget": "example.widgets.widget"}


def test_no_routing_file_means_no_redirects() -> None:
    assert load_redirects(detect_project(BUILT)) == {}


def test_entries_without_redirect_are_ignored(tmp_path: Path) -> None:
    runtime = """
plugin_routing:
  modules:
    gone:
      tombstone: {removal_version: "2.0.0", warning_text: removed}
    deprecated_only:
      deprecation: {removal_version: "3.0.0", warning_text: soon}
    moved:
      redirect: ns.coll.target
      deprecation: {removal_version: "3.0.0", warning_text: renamed}
  lookup:
    other_plugin_type:
      redirect: ns.coll.whatever
"""
    project = detect_project(_collection(tmp_path, "target", runtime=runtime))
    assert load_redirects(project) == {"moved": "ns.coll.target"}


@pytest.mark.parametrize(
    "runtime",
    [
        "plugin_routing: [unclosed\n",
        "plugin_routing:\n  modules:\n    x:\n      redirect: 3\n",
        "plugin_routing:\n  modules: [a, b]\n",
        "- just a list\n",
        "plugin_routing: [a, b]\n",
    ],
)
def test_malformed_routing_file_is_an_error(tmp_path: Path, runtime: str) -> None:
    project = detect_project(_collection(tmp_path, "target", runtime=runtime))
    with pytest.raises(DiscoveryError, match=re.escape("runtime.yml")):
        load_redirects(project)


def test_empty_routing_file_means_no_redirects(tmp_path: Path) -> None:
    project = detect_project(_collection(tmp_path, "target", runtime=""))
    assert load_redirects(project) == {}


# --- aliases on discovered modules -------------------------------------------------


def test_core_module_answers_to_legacy_and_redirected_names() -> None:
    incremental = _modules(CORE)["incremental"]
    assert incremental.aliases == (
        "ansible.builtin.old_incremental",
        "ansible.legacy.incremental",
        "ansible.legacy.old_incremental",
        "old_incremental",
    )


def test_every_core_module_has_its_legacy_name() -> None:
    for name, info in _modules(CORE).items():
        assert f"ansible.legacy.{name}" in info.aliases


def test_collection_module_answers_to_redirected_names() -> None:
    widget = _modules(COLLECTION)["widget"]
    assert widget.aliases == ("example.widgets.old_widget", "old_widget")


def test_module_names_include_name_fqcn_and_aliases() -> None:
    widget = _modules(COLLECTION)["widget"]
    assert widget.names == (
        "example.widgets.old_widget",
        "example.widgets.widget",
        "old_widget",
        "widget",
    )


def test_redirect_chains_are_followed(tmp_path: Path) -> None:
    runtime = """
plugin_routing:
  modules:
    oldest:
      redirect: ns.coll.older
    older:
      redirect: ns.coll.target
"""
    target = _modules(_collection(tmp_path, "target", runtime=runtime))["target"]
    assert {"older", "oldest", "ns.coll.older", "ns.coll.oldest"} <= set(target.aliases)


def test_redirect_loop_is_an_error(tmp_path: Path) -> None:
    runtime = """
plugin_routing:
  modules:
    a:
      redirect: ns.coll.b
    b:
      redirect: ns.coll.a
"""
    project = detect_project(_collection(tmp_path, "target", runtime=runtime))
    with pytest.raises(DiscoveryError, match="loop"):
        discover_modules(project)


def test_redirect_to_another_collection_adds_no_alias(tmp_path: Path) -> None:
    runtime = """
plugin_routing:
  modules:
    moved_away:
      redirect: other.coll.moved_away
"""
    target = _modules(_collection(tmp_path, "target", runtime=runtime))["target"]
    assert target.aliases == ()


def test_redirect_named_like_a_real_module_is_ignored(tmp_path: Path) -> None:
    runtime = """
plugin_routing:
  modules:
    real:
      redirect: ns.coll.target
"""
    modules = _modules(_collection(tmp_path, "target", "real", runtime=runtime))
    assert "real" not in modules["target"].aliases
    assert "real" in modules


def test_core_legacy_redirect_targets_are_normalised(tmp_path: Path) -> None:
    runtime = """
plugin_routing:
  modules:
    old:
      redirect: ansible.legacy.target
"""
    target = _modules(_core(tmp_path, "target", runtime=runtime))["target"]
    assert "old" in target.aliases
    assert "ansible.builtin.old" in target.aliases


def test_symlinked_module_is_an_alias_not_a_module(tmp_path: Path) -> None:
    root = _core(tmp_path, "target")
    modules_dir = root / "lib" / "ansible" / "modules"
    (modules_dir / "linked.py").symlink_to("target.py")
    modules = _modules(root)
    assert list(modules) == ["target"]
    assert {"linked", "ansible.builtin.linked", "ansible.legacy.linked"} <= set(
        modules["target"].aliases
    )


def test_symlink_pointing_outside_the_modules_dir_is_a_module(tmp_path: Path) -> None:
    root = _core(tmp_path, "target")
    outside = tmp_path / "elsewhere.py"
    outside.write_text("")
    (root / "lib" / "ansible" / "modules" / "external.py").symlink_to(outside)
    assert list(_modules(root)) == ["external", "target"]


# --- filter_modules (--module) ------------------------------------------------------


def _core_modules() -> list[ModuleInfo]:
    return discover_modules(detect_project(CORE))


@pytest.mark.parametrize(
    "pattern",
    [
        "incremental",
        "ansible.builtin.incremental",
        "ansible.legacy.incremental",
        "old_incremental",
        "ansible.builtin.old_incremental",
    ],
)
def test_filter_by_any_name(pattern: str) -> None:
    assert [m.name for m in filter_modules(_core_modules(), [pattern])] == [
        "incremental"
    ]


def test_filter_by_glob() -> None:
    selected = filter_modules(_core_modules(), ["h*"])
    assert [m.name for m in selected] == ["helper", "hybrid"]


def test_filter_by_fqcn_glob() -> None:
    selected = filter_modules(_core_modules(), ["ansible.builtin.in*"])
    assert [m.name for m in selected] == ["include_tasks", "incremental", "invalid"]


def test_filter_union_keeps_sorted_order_without_duplicates() -> None:
    selected = filter_modules(_core_modules(), ["virtual", "h*", "hybrid"])
    assert [m.name for m in selected] == ["helper", "hybrid", "virtual"]


def test_no_patterns_selects_everything() -> None:
    modules = _core_modules()
    assert filter_modules(modules, []) == modules


def test_unknown_literal_name_is_an_error() -> None:
    with pytest.raises(DiscoveryError, match="nosuchmodule"):
        filter_modules(_core_modules(), ["incremental", "nosuchmodule"])


def test_glob_matching_nothing_is_not_an_error() -> None:
    assert filter_modules(_core_modules(), ["zz*"]) == []


# --- determinism -------------------------------------------------------------------


def test_discovery_is_deterministic() -> None:
    project: Project = detect_project(CORE)
    assert discover_modules(project) == discover_modules(project)
