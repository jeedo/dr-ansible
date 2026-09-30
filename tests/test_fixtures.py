"""Contract tests for the synthetic fixtures under tests/fixtures (plan task 6).

The fixtures are test data for later tasks: small modules, action plugins,
docs and integration targets, one per pattern dr-ansible must handle. These
tests pin down what each fixture demonstrates, so a later edit cannot quietly
remove the pattern a fixture exists for. They inspect fixtures with ``ast``
and YAML parsing only; the single exception is the import trap, which is
imported on purpose to prove it is armed.
"""

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CORE = FIXTURES / "core"
CORE_MODULES = CORE / "lib" / "ansible" / "modules"
CORE_ACTIONS = CORE / "lib" / "ansible" / "plugins" / "action"
CORE_TARGETS = CORE / "test" / "integration" / "targets"
COLLECTION = FIXTURES / "collection"
BUILT_COLLECTION = FIXTURES / "built_collection"

# Expected RETURN status of each Python module in the core fixture tree.
CORE_RETURN_STATUS = {
    "dynamic": "missing",
    "helper": "present",
    "hybrid": "present",
    "include_tasks": "missing",  # exempt through the default allowlist
    "incremental": "missing",
    "invalid": "invalid",
    "keywords": "placeholder",
    "nested": "present",
    "raises_on_import": "missing",
    "sidecar": "sidecar",  # docs live in sidecar.yml
    "virtual": "missing",
}


# --- helpers -------------------------------------------------------------------


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def _return_text(path: Path) -> str | None:
    """The module-level RETURN string, or None if there is no assignment."""
    for node in _tree(path).body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "RETURN" for t in node.targets)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
    return None


def _calls(tree: ast.AST, attr: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == attr
    ]


def _keywords(call: ast.Call) -> set[str]:
    return {k.arg for k in call.keywords if k.arg is not None}


def _splats(call: ast.Call) -> list[str]:
    return [
        k.value.id
        for k in call.keywords
        if k.arg is None and isinstance(k.value, ast.Name)
    ]


def _all_files() -> list[Path]:
    return sorted(p for p in FIXTURES.rglob("*") if p.is_file())


# --- whole tree ----------------------------------------------------------------


def test_fixture_tree_exists() -> None:
    assert CORE_MODULES.is_dir()
    assert (COLLECTION / "galaxy.yml").is_file()
    assert (BUILT_COLLECTION / "MANIFEST.json").is_file()


def test_every_python_fixture_is_valid_python() -> None:
    py_files = [p for p in _all_files() if p.suffix == ".py"]
    assert py_files
    for path in py_files:
        _tree(path)  # raises SyntaxError if not


def test_every_yaml_fixture_parses_except_invalid_on_purpose() -> None:
    yaml_files = [p for p in _all_files() if p.suffix in {".yml", ".yaml"}]
    assert yaml_files
    for path in yaml_files:
        yaml.safe_load(path.read_text())


def test_no_fixture_is_gitignored() -> None:
    root = FIXTURES.parent.parent
    files = [str(p.relative_to(root)) for p in _all_files()]
    result = subprocess.run(
        ["git", "-c", f"safe.directory={root}", "check-ignore", "--no-index", *files],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stdout == ""


def test_readme_documents_every_module_fixture() -> None:
    readme = (FIXTURES / "README.md").read_text()
    modules = [
        p.stem
        for p in _all_files()
        if p.parent.name == "modules" and p.suffix in {".py", ".ps1"}
        if p.stem != "__init__"
    ]
    for name in modules:
        assert f"`{name}`" in readme, name


# --- core: RETURN statuses -----------------------------------------------------


def test_core_modules_are_exactly_the_expected_set() -> None:
    names = {p.stem for p in CORE_MODULES.glob("*.py") if p.name != "__init__.py"}
    assert names == set(CORE_RETURN_STATUS)
    assert (CORE_MODULES / "__init__.py").is_file()  # discovery must skip it


@pytest.mark.parametrize(
    ("name", "status"), sorted(CORE_RETURN_STATUS.items()), ids=lambda v: str(v)
)
def test_core_module_return_status(name: str, status: str) -> None:
    text = _return_text(CORE_MODULES / f"{name}.py")
    if status in {"missing", "sidecar"}:
        assert text is None
    elif status == "placeholder":
        assert text is not None
        assert yaml.safe_load(text) is None
    elif status == "invalid":
        assert text is not None
        with pytest.raises(yaml.YAMLError):
            yaml.safe_load(text)
    else:
        assert text is not None
        docs = yaml.safe_load(text)
        assert isinstance(docs, dict)
        assert docs


def test_present_docs_have_the_required_fields() -> None:
    for name, status in CORE_RETURN_STATUS.items():
        if status != "present":
            continue
        text = _return_text(CORE_MODULES / f"{name}.py")
        assert text is not None
        for key, entry in yaml.safe_load(text).items():
            assert {"description", "returned", "type"} <= set(entry), (name, key)


def test_nested_module_documents_contains() -> None:
    text = _return_text(CORE_MODULES / "nested.py")
    assert text is not None
    info = yaml.safe_load(text)["info"]
    assert info["type"] == "complex"
    assert {"exists", "size", "owner"} <= set(info["contains"])


def test_helper_module_has_an_undocumented_and_a_stale_key() -> None:
    text = _return_text(CORE_MODULES / "helper.py")
    assert text is not None
    documented = set(yaml.safe_load(text))
    assert "legacy_id" in documented  # stale: never returned
    assert "owner" not in documented  # undocumented: returned by the helper


def test_sidecar_docs_live_in_the_yml_file() -> None:
    docs = yaml.safe_load((CORE_MODULES / "sidecar.yml").read_text())
    assert set(docs) >= {"DOCUMENTATION", "RETURN"}
    assert isinstance(docs["RETURN"], dict)
    assigned = {
        t.id
        for n in _tree(CORE_MODULES / "sidecar.py").body
        if isinstance(n, ast.Assign)
        for t in n.targets
        if isinstance(t, ast.Name)
    }
    assert not assigned & {"DOCUMENTATION", "RETURN"}


# --- core: return patterns -----------------------------------------------------


def test_incremental_builds_result_step_by_step() -> None:
    tree = _tree(CORE_MODULES / "incremental.py")
    (exit_call,) = _calls(tree, "exit_json")
    assert _splats(exit_call) == ["result"]
    assert _calls(tree, "update")
    assert _calls(tree, "setdefault")
    subscripts = [
        t
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        for t in n.targets
        if isinstance(t, ast.Subscript)
    ]
    assert subscripts
    conditional = [n for n in ast.walk(tree) if isinstance(n, ast.If)]
    assert conditional  # a key set only under a condition


def test_keywords_module_has_failure_only_keys() -> None:
    tree = _tree(CORE_MODULES / "keywords.py")
    success = set().union(*(_keywords(c) for c in _calls(tree, "exit_json")))
    failure = set().union(*(_keywords(c) for c in _calls(tree, "fail_json")))
    assert {"ping", "count", "ratio", "items", "enabled"} <= success
    assert failure - success >= {"rc", "stderr"}


def test_helper_module_returns_a_helper_dict() -> None:
    tree = _tree(CORE_MODULES / "helper.py")
    helpers = [
        f for f in tree.body if isinstance(f, ast.FunctionDef) and f.name != "main"
    ]
    assert any(
        isinstance(n, ast.Return) and isinstance(n.value, ast.Dict)
        for f in helpers
        for n in ast.walk(f)
    )
    (exit_call,) = _calls(tree, "exit_json")
    assert _splats(exit_call)


def test_virtual_module_is_docs_only_with_an_action_plugin() -> None:
    tree = _tree(CORE_MODULES / "virtual.py")
    assert not [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    action = _tree(CORE_ACTIONS / "virtual.py")
    run = [
        f
        for c in action.body
        if isinstance(c, ast.ClassDef) and c.name == "ActionModule"
        for f in c.body
        if isinstance(f, ast.FunctionDef) and f.name == "run"
    ]
    assert run
    assert not _calls(action, "_execute_module")
    assert _calls(action, "update")
    assert any(isinstance(n, ast.Return) for n in ast.walk(run[0]))


def test_hybrid_action_plugin_executes_the_module() -> None:
    action = _tree(CORE_ACTIONS / "hybrid.py")
    (call,) = _calls(action, "_execute_module")
    assert any(k.arg == "module_name" for k in call.keywords)
    assert _calls(action, "update")
    assert _calls(_tree(CORE_MODULES / "hybrid.py"), "exit_json")


def test_dynamic_module_has_unresolvable_keys() -> None:
    tree = _tree(CORE_MODULES / "dynamic.py")
    computed = [
        t
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        for t in n.targets
        if isinstance(t, ast.Subscript) and not isinstance(t.slice, ast.Constant)
    ]
    assert computed
    funcs = {f.name: f for f in tree.body if isinstance(f, ast.FunctionDef)}
    kwarg = funcs["finish"].args.kwarg
    assert kwarg is not None
    splat_from_kwargs = [
        c for c in _calls(funcs["finish"], "exit_json") if kwarg.arg in _splats(c)
    ]
    assert splat_from_kwargs


def test_powershell_module_has_no_python_twin() -> None:
    ps1 = CORE_MODULES / "win_ping.ps1"
    assert ps1.is_file()
    assert not ps1.with_suffix(".py").exists()


def test_import_trap_is_armed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Importing would otherwise write __pycache__ into the fixture tree.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    path = CORE_MODULES / "raises_on_import.py"
    spec = importlib.util.spec_from_file_location("dr_ansible_import_trap", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with pytest.raises(RuntimeError, match="must never import"):
        spec.loader.exec_module(module)
    # The code after the trap still has a return pattern for static analysis.
    assert _calls(_tree(path), "exit_json")


# --- core: routing and integration targets -------------------------------------


def test_core_runtime_file_redirects_a_module() -> None:
    runtime = yaml.safe_load(
        (
            CORE / "lib" / "ansible" / "config" / "ansible_builtin_runtime.yml"
        ).read_text()
    )
    modules = runtime["plugin_routing"]["modules"]
    assert modules["old_incremental"]["redirect"] == "ansible.builtin.incremental"


def _tasks(target: Path) -> list[dict[str, object]]:
    tasks: list[dict[str, object]] = []
    for path in sorted(target.rglob("*.yml")):
        data = yaml.safe_load(path.read_text())
        if isinstance(data, list):
            tasks.extend(t for t in data if isinstance(t, dict))
    return tasks


@pytest.mark.parametrize("target", ["incremental", "virtual", "hybrid", "keywords"])
def test_integration_target_registers_and_asserts(target: str) -> None:
    tasks = _tasks(CORE_TARGETS / target)
    registered = {str(t["register"]) for t in tasks if "register" in t}
    assert registered
    asserts = [str(t["assert"]) for t in tasks if "assert" in t]
    assert any(var in text for var in registered for text in asserts)


def test_integration_targets_use_several_module_names() -> None:
    tasks = _tasks(CORE_TARGETS / "incremental")
    names = {k for t in tasks for k in t if "incremental" in k}
    assert names >= {"incremental", "ansible.builtin.incremental"}


def test_hybrid_target_uses_the_roles_layout() -> None:
    assert list((CORE_TARGETS / "hybrid").glob("roles/*/tasks/main.yml"))


def test_keywords_target_has_no_log_and_secret_values() -> None:
    tasks = _tasks(CORE_TARGETS / "keywords")
    assert any(t.get("no_log") is True for t in tasks)
    text = (CORE_TARGETS / "keywords" / "tasks" / "main.yml").read_text()
    assert "password" in text


# --- collections -----------------------------------------------------------------


def test_collection_identity_from_galaxy_yml() -> None:
    galaxy = yaml.safe_load((COLLECTION / "galaxy.yml").read_text())
    assert (galaxy["namespace"], galaxy["name"]) == ("example", "widgets")
    assert (COLLECTION / "plugins" / "modules" / "widget.py").is_file()
    assert _return_text(COLLECTION / "plugins" / "modules" / "widget.py")


def test_collection_runtime_redirects_and_has_a_target() -> None:
    runtime = yaml.safe_load((COLLECTION / "meta" / "runtime.yml").read_text())
    redirect = runtime["plugin_routing"]["modules"]["old_widget"]["redirect"]
    assert redirect == "example.widgets.widget"
    target = COLLECTION / "tests" / "integration" / "targets" / "widget"
    assert _tasks(target)


def test_built_collection_identity_from_manifest_json() -> None:
    manifest = json.loads((BUILT_COLLECTION / "MANIFEST.json").read_text())
    info = manifest["collection_info"]
    assert (info["namespace"], info["name"]) == ("example", "gadgets")
    assert not (BUILT_COLLECTION / "galaxy.yml").exists()
    assert (BUILT_COLLECTION / "plugins" / "modules" / "gadget.py").is_file()
