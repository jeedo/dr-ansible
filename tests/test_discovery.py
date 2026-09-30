"""Tests for layout detection and module discovery (plan task 8: FR-1, FR-2)."""

import json
import re
from pathlib import Path

import pytest

from dr_ansible.discovery import (
    DiscoveryError,
    Layout,
    Project,
    detect_project,
    discover_modules,
)
from dr_ansible.model import Language, ModuleInfo

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CORE = FIXTURES / "core"
COLLECTION = FIXTURES / "collection"
BUILT = FIXTURES / "built_collection"


def _by_name(project: Project) -> dict[str, ModuleInfo]:
    return {m.name: m for m in discover_modules(project)}


# --- detect_project: ansible-core ------------------------------------------------


def test_detects_ansible_core_checkout() -> None:
    project = detect_project(CORE)
    assert project.layout is Layout.CORE
    assert (project.namespace, project.name) == ("ansible", "builtin")
    assert project.collection == "ansible.builtin"
    assert project.root == CORE
    assert project.modules_dir == CORE / "lib" / "ansible" / "modules"
    assert project.actions_dir == CORE / "lib" / "ansible" / "plugins" / "action"
    assert project.targets_dir == CORE / "test" / "integration" / "targets"


def test_relative_paths_are_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(FIXTURES)
    assert detect_project(Path("core")).root == CORE


# --- detect_project: collections -------------------------------------------------


def test_detects_source_collection_from_galaxy_yml() -> None:
    project = detect_project(COLLECTION)
    assert project.layout is Layout.COLLECTION
    assert project.collection == "example.widgets"
    assert project.modules_dir == COLLECTION / "plugins" / "modules"
    assert project.actions_dir == COLLECTION / "plugins" / "action"
    assert project.targets_dir == COLLECTION / "tests" / "integration" / "targets"


def test_detects_built_collection_from_manifest_json() -> None:
    project = detect_project(BUILT)
    assert project.layout is Layout.COLLECTION
    assert project.collection == "example.gadgets"


def test_galaxy_yml_wins_over_manifest_json(tmp_path: Path) -> None:
    (tmp_path / "galaxy.yml").write_text("namespace: src\nname: coll\n")
    (tmp_path / "MANIFEST.json").write_text(
        json.dumps({"collection_info": {"namespace": "built", "name": "coll"}})
    )
    assert detect_project(tmp_path).collection == "src.coll"


def test_core_layout_wins_over_collection_metadata(tmp_path: Path) -> None:
    (tmp_path / "lib" / "ansible" / "modules").mkdir(parents=True)
    (tmp_path / "galaxy.yml").write_text("namespace: a\nname: b\n")
    assert detect_project(tmp_path).layout is Layout.CORE


# --- detect_project: errors ------------------------------------------------------


def test_unrecognised_directory_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(DiscoveryError, match="not an ansible-core checkout"):
        detect_project(tmp_path)


def test_missing_path_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(DiscoveryError, match="does not exist"):
        detect_project(tmp_path / "nope")


def test_file_path_is_an_error(tmp_path: Path) -> None:
    file = tmp_path / "galaxy.yml"
    file.write_text("namespace: a\nname: b\n")
    with pytest.raises(DiscoveryError, match="not a directory"):
        detect_project(file)


@pytest.mark.parametrize(
    "content",
    [
        "name: only_name\n",
        "namespace: only_namespace\n",
        "namespace: ''\nname: x\n",
        "namespace: 3\nname: x\n",
        "- a list\n",
        "namespace: [unclosed\n",
        "namespace: Bad-Name\nname: x\n",
    ],
)
def test_bad_galaxy_yml_is_an_error(tmp_path: Path, content: str) -> None:
    (tmp_path / "galaxy.yml").write_text(content)
    with pytest.raises(DiscoveryError, match=re.escape("galaxy.yml")):
        detect_project(tmp_path)


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        json.dumps({"namespace": "a", "name": "b"}),
        json.dumps({"collection_info": {"namespace": "a"}}),
        json.dumps([1, 2]),
    ],
)
def test_bad_manifest_json_is_an_error(tmp_path: Path, content: str) -> None:
    (tmp_path / "MANIFEST.json").write_text(content)
    with pytest.raises(DiscoveryError, match=re.escape("MANIFEST.json")):
        detect_project(tmp_path)


def test_discovery_error_is_a_value_error() -> None:
    assert issubclass(DiscoveryError, ValueError)


# --- discover_modules: ansible-core fixture --------------------------------------


def test_lists_every_core_module_sorted_and_skips_init() -> None:
    modules = discover_modules(detect_project(CORE))
    names = [m.name for m in modules]
    assert names == sorted(names)
    assert names == [
        "dynamic",
        "helper",
        "hybrid",
        "include_tasks",
        "incremental",
        "invalid",
        "keywords",
        "nested",
        "raises_on_import",
        "sidecar",
        "virtual",
        "win_ping",
    ]


def test_core_modules_get_ansible_builtin_fqcns() -> None:
    modules = _by_name(detect_project(CORE))
    assert modules["incremental"].fqcn == "ansible.builtin.incremental"
    assert all(m.fqcn == f"ansible.builtin.{name}" for name, m in modules.items())


def test_python_module_paths() -> None:
    incremental = _by_name(detect_project(CORE))["incremental"]
    assert incremental.language is Language.PYTHON
    assert incremental.module_path == CORE / "lib/ansible/modules/incremental.py"
    assert incremental.test_target == CORE / "test/integration/targets/incremental"
    assert incremental.action_path is None
    assert incremental.sidecar_path is None


def test_pairs_action_plugins() -> None:
    modules = _by_name(detect_project(CORE))
    actions = CORE / "lib/ansible/plugins/action"
    assert modules["virtual"].action_path == actions / "virtual.py"
    assert modules["hybrid"].action_path == actions / "hybrid.py"
    assert [n for n, m in modules.items() if m.action_path] == ["hybrid", "virtual"]


def test_pairs_sidecar_docs() -> None:
    sidecar = _by_name(detect_project(CORE))["sidecar"]
    assert sidecar.sidecar_path == CORE / "lib/ansible/modules/sidecar.yml"


def test_pairs_integration_targets_including_roles_layout() -> None:
    modules = _by_name(detect_project(CORE))
    with_targets = sorted(n for n, m in modules.items() if m.test_target)
    assert with_targets == ["hybrid", "incremental", "keywords", "virtual"]


def test_powershell_module_is_listed_as_powershell() -> None:
    win_ping = _by_name(detect_project(CORE))["win_ping"]
    assert win_ping.language is Language.POWERSHELL
    assert win_ping.module_path == CORE / "lib/ansible/modules/win_ping.ps1"


def test_discovery_never_imports_module_code() -> None:
    # raises_on_import.py raises if imported; discovery must still list it.
    assert "raises_on_import" in _by_name(detect_project(CORE))


# --- discover_modules: collections ------------------------------------------------


def test_collection_modules_get_collection_fqcns() -> None:
    (widget,) = discover_modules(detect_project(COLLECTION))
    assert widget.fqcn == "example.widgets.widget"
    assert widget.test_target == COLLECTION / "tests/integration/targets/widget"


def test_built_collection_modules() -> None:
    (gadget,) = discover_modules(detect_project(BUILT))
    assert gadget.fqcn == "example.gadgets.gadget"
    assert gadget.test_target is None


# --- discover_modules: edge cases (tmp trees) -------------------------------------


def _collection(tmp_path: Path) -> Path:
    (tmp_path / "galaxy.yml").write_text("namespace: ns\nname: coll\n")
    modules = tmp_path / "plugins" / "modules"
    modules.mkdir(parents=True)
    return modules


def test_powershell_with_python_docs_stub_is_one_powershell_module(
    tmp_path: Path,
) -> None:
    modules = _collection(tmp_path)
    (modules / "win_thing.ps1").write_text("#!powershell\n")
    (modules / "win_thing.py").write_text('DOCUMENTATION = r"""\nmodule: x\n"""\n')
    (info,) = discover_modules(detect_project(tmp_path))
    assert info.language is Language.POWERSHELL
    assert info.module_path == modules / "win_thing.ps1"
    assert info.sidecar_path is None


def test_yaml_extension_sidecar_is_paired(tmp_path: Path) -> None:
    modules = _collection(tmp_path)
    (modules / "thing.py").write_text("")
    (modules / "thing.yaml").write_text("RETURN: {}\n")
    (info,) = discover_modules(detect_project(tmp_path))
    assert info.sidecar_path == modules / "thing.yaml"


def test_ignores_non_module_files_and_directories(tmp_path: Path) -> None:
    modules = _collection(tmp_path)
    (modules / "real.py").write_text("")
    (modules / "__init__.py").write_text("")
    (modules / "notes.txt").write_text("")
    (modules / "orphan.yml").write_text("RETURN: {}\n")
    (modules / "__pycache__").mkdir()
    (modules / "__pycache__" / "real.cpython-313.pyc").write_bytes(b"")
    (modules / "subdir").mkdir()
    (modules / "subdir" / "nested.py").write_text("")
    assert [m.name for m in discover_modules(detect_project(tmp_path))] == ["real"]


def test_target_must_be_a_directory(tmp_path: Path) -> None:
    modules = _collection(tmp_path)
    (modules / "thing.py").write_text("")
    targets = tmp_path / "tests" / "integration" / "targets"
    targets.mkdir(parents=True)
    (targets / "thing").write_text("not a directory")
    (info,) = discover_modules(detect_project(tmp_path))
    assert info.test_target is None


def test_collection_without_modules_dir_has_no_modules(tmp_path: Path) -> None:
    (tmp_path / "galaxy.yml").write_text("namespace: ns\nname: empty\n")
    assert discover_modules(detect_project(tmp_path)) == []
