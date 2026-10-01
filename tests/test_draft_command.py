"""Tests for the ``draft`` command (plan task 25: FR-19, FR-20, NFR-5)."""

import hashlib
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner, Result

from dr_ansible.cli import main
from dr_ansible.config import Config
from dr_ansible.discovery import detect_project, discover_modules
from dr_ansible.draft import MARKER, render_draft, render_merge
from dr_ansible.pipeline import analyze

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CORE = FIXTURES / "core"


def _draft(*args: str | Path) -> Result:
    return CliRunner().invoke(main, ["draft", *map(str, args)])


def _expected(name: str, *, full: bool) -> str:
    project = detect_project(CORE)
    module = {m.name: m for m in discover_modules(project)}[name]
    result = analyze(module, Config())
    if full:
        return render_draft(result.report, Config(), project.root)
    assert result.docs is not None
    return render_merge(result.report, result.docs, Config(), project.root)


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


# --- output -------------------------------------------------------------------------


def test_merge_is_the_default() -> None:
    result = _draft(CORE, "helper")
    assert result.exit_code == 0, result.output
    assert result.stdout == _expected("helper", full=False)
    assert "name:" in result.stdout  # the existing entries are kept


def test_merge_flag() -> None:
    assert _draft(CORE, "helper", "--merge").stdout == _expected("helper", full=False)


def test_full_draft() -> None:
    result = _draft(CORE, "helper", "--full")
    assert result.exit_code == 0
    assert result.stdout == _expected("helper", full=True)
    assert result.stdout != _expected("helper", full=False)


def test_missing_return_gets_a_full_draft_in_merge_mode() -> None:
    result = _draft(CORE, "incremental")
    assert result.exit_code == 0
    assert "# no existing RETURN to merge into: this is a full draft" in result.stdout
    assert MARKER in result.stdout


def test_draft_is_valid_python_with_a_yaml_return() -> None:
    namespace: dict[str, object] = {}
    exec(_draft(CORE, "virtual").stdout, namespace)  # the draft, never the module
    entries = yaml.safe_load(str(namespace["RETURN"]))
    assert set(entries) == {"checksum", "dest", "file", "remote_checksum", "src"}


def test_module_by_alias() -> None:
    assert _draft(CORE, "old_incremental").stdout == _draft(CORE, "incremental").stdout


def test_exempt_modules_can_still_be_drafted() -> None:
    assert _draft(CORE, "include_tasks").exit_code == 0


# --- --output -----------------------------------------------------------------------


def test_output_writes_the_file_and_nothing_to_stdout(tmp_path: Path) -> None:
    target = tmp_path / "helper_return.py"
    result = _draft(CORE, "helper", "--output", target)
    assert result.exit_code == 0
    assert result.stdout == ""
    assert target.read_text() == _expected("helper", full=False)
    assert f"wrote draft for ansible.builtin.helper to {target}" in result.stderr


def test_output_overwrites_an_existing_draft(tmp_path: Path) -> None:
    target = tmp_path / "draft.py"
    target.write_text("old\n")
    assert _draft(CORE, "helper", "--full", "-o", target).exit_code == 0
    assert target.read_text() == _expected("helper", full=True)


@pytest.mark.parametrize(
    "target",
    [
        "lib/ansible/modules/helper.py",  # the module itself
        "lib/ansible/modules/new_file.py",  # anywhere among the modules
        "lib/ansible/plugins/action/draft.py",  # or the action plugins
    ],
)
def test_output_never_writes_into_module_directories(target: str) -> None:
    before = _tree_digest(CORE)
    result = _draft(CORE, "helper", "--output", CORE / target)
    assert result.exit_code == 2
    assert "refusing to write" in result.stderr
    assert _tree_digest(CORE) == before


def test_output_to_an_unwritable_place_is_an_error(tmp_path: Path) -> None:
    result = _draft(CORE, "helper", "--output", tmp_path / "missing_dir" / "x.py")
    assert result.exit_code == 2
    assert "cannot write" in result.stderr


def test_drafting_never_changes_the_analysed_tree() -> None:
    before = _tree_digest(CORE)
    for name in ("helper", "incremental", "sidecar", "keywords", "virtual"):
        assert _draft(CORE, name).exit_code == 0
        assert _draft(CORE, name, "--full").exit_code == 0
    assert _tree_digest(CORE) == before


# --- errors -------------------------------------------------------------------------


def test_unsupported_module_is_an_error() -> None:
    result = _draft(CORE, "win_ping")
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "ansible.builtin.win_ping: PowerShell modules are not supported" in (
        result.stderr
    )


def test_a_broken_module_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "galaxy.yml").write_text("namespace: ns\nname: coll\nversion: 1.0.0\n")
    modules = tmp_path / "plugins" / "modules"
    modules.mkdir(parents=True)
    (modules / "broken.py").write_text("def main(:\n")
    result = _draft(tmp_path, "broken")
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "cannot draft ns.coll.broken:" in result.stderr
    assert "broken.py" in result.stderr


def test_unknown_module_is_a_usage_error() -> None:
    result = _draft(CORE, "no_such_module")
    assert result.exit_code == 2
    assert "no module named 'no_such_module'" in result.stderr


def test_merge_and_full_are_one_switch() -> None:
    # The last of --merge / --full wins, as for any click boolean flag pair.
    assert _draft(CORE, "helper", "--merge", "--full").stdout == _expected(
        "helper", full=True
    )
