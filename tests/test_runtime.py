"""Tests for runtime mining with ``--run`` (plan task 27: FR-14, FR-16, NFR-5)."""

import configparser
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from dr_ansible.cli import main
from dr_ansible.config import Config
from dr_ansible.discovery import detect_project, discover_modules
from dr_ansible.mining import runtime
from dr_ansible.mining.callback import CALLBACK_NAME
from dr_ansible.mining.runtime import (
    RECORDING_NAME,
    RunError,
    build_command,
    load_recording,
    prepare_copy,
    run_integration,
)
from dr_ansible.model import Location, ModuleInfo, ResultState, Sample

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CORE = FIXTURES / "core"

MODULE = """\
#!/usr/bin/python
from ansible.module_utils.basic import AnsibleModule


def main():
    module = AnsibleModule(argument_spec=dict(name=dict(type="str", default="x")))
    module.exit_json(changed=False, name=module.params["name"], info={"size": 3})


if __name__ == "__main__":
    main()
"""
TASKS = """\
- ns.coll.thing:
    name: hello
  register: result
- assert:
    that: result.name == 'hello'
"""


def _collection(tmp_path: Path) -> Path:
    root = tmp_path / "src" / "ansible_collections" / "ns" / "coll"
    (root / "plugins" / "modules").mkdir(parents=True)
    (root / "galaxy.yml").write_text(
        "namespace: ns\nname: coll\nversion: 1.0.0\nreadme: README.md\nauthors: [x]\n"
    )
    (root / "README.md").write_text("")
    (root / "plugins" / "modules" / "thing.py").write_text(MODULE)
    tasks = root / "tests" / "integration" / "targets" / "thing" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "main.yml").write_text(TASKS)
    (root / "tests" / "output" / "junk").mkdir(parents=True)  # results: not copied
    return root


def _module(root: Path, name: str) -> tuple[Any, ModuleInfo]:
    project = detect_project(root)
    return project, {m.name: m for m in discover_modules(project)}[name]


def _digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _record(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "version": 1,
        "module": "ns.coll.thing",
        "state": "ok",
        "task": "thing : ns.coll.thing",
        "location": {
            "file": "/root/ansible_collections/ns/coll/tests/output/.tmp/integration/"
            "thing-abc-ÅÑ/tests/integration/targets/thing/tasks/main.yml",
            "line": 1,
        },
        "keys": [
            {"path": "name", "type": "str", "value": "hello"},
            {"path": "info", "type": "dict"},
            {"path": "info.size", "type": "int", "value": 3},
        ],
    }
    record.update(overrides)
    return record


def _write(path: Path, records: list[dict[str, Any] | str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [r if isinstance(r, str) else json.dumps(r) for r in records]
    path.write_text("".join(f"{line}\n" for line in lines))
    return path


# --- preparing the copy -------------------------------------------------------------


def test_copy_of_a_collection_keeps_its_layout(tmp_path: Path) -> None:
    project, module = _module(_collection(tmp_path), "thing")
    copy = prepare_copy(project, module, Config(), tmp_path / "work")
    assert copy == tmp_path / "work" / "ansible_collections" / "ns" / "coll"
    assert (copy / "plugins" / "modules" / "thing.py").read_text() == MODULE
    assert not (copy / "tests" / "output").exists()


def test_copy_of_ansible_core(tmp_path: Path) -> None:
    project, module = _module(CORE, "incremental")
    copy = prepare_copy(project, module, Config(), tmp_path / "work")
    assert (copy / "lib" / "ansible" / "modules" / "incremental.py").is_file()
    assert (copy / "test" / "integration" / "dr_ansible").is_dir()


def test_the_copy_auto_enables_the_recorder(tmp_path: Path) -> None:
    project, module = _module(_collection(tmp_path), "thing")
    copy = prepare_copy(project, module, Config(), tmp_path / "work")
    plugin = copy / "tests" / "integration" / "dr_ansible" / "callback_plugins"
    source = (plugin / f"{CALLBACK_NAME}.py").read_text()
    assert "\nAUTO_ENABLE = True\n" in source
    assert "\nAUTO_ENABLE = False\n" not in source


def test_the_copy_configures_the_recorder(tmp_path: Path) -> None:
    project, module = _module(_collection(tmp_path), "thing")
    config = Config(redact_patterns=("pin",))
    copy = prepare_copy(project, module, config, tmp_path / "work")
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(copy / "tests" / "integration" / "integration.cfg")
    assert parser["defaults"]["callback_plugins"] == (
        "$JUNIT_OUTPUT_DIR/../../integration/dr_ansible/callback_plugins"
    )
    section = parser["callback_dr_ansible_recorder"]
    assert section["output"] == f"$JUNIT_OUTPUT_DIR/../data/{RECORDING_NAME}"
    assert section["module_names"] == "ns.coll.thing, thing"
    assert section["redact_patterns"] == "pin"


def test_an_existing_integration_cfg_is_extended(tmp_path: Path) -> None:
    root = _collection(tmp_path)
    (root / "tests" / "integration" / "integration.cfg").write_text(
        "[defaults]\ncallback_plugins = mine\ntimeout = 30\n"
    )
    project, module = _module(root, "thing")
    copy = prepare_copy(project, module, Config(), tmp_path / "work")
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(copy / "tests" / "integration" / "integration.cfg")
    assert parser["defaults"]["timeout"] == "30"
    assert parser["defaults"]["callback_plugins"] == (
        "$JUNIT_OUTPUT_DIR/../../integration/dr_ansible/callback_plugins:mine"
    )


def test_preparing_never_touches_the_project(tmp_path: Path) -> None:
    root = _collection(tmp_path)
    before = _digest(root)
    project, module = _module(root, "thing")
    prepare_copy(project, module, Config(), tmp_path / "work")
    assert _digest(root) == before


def test_a_module_without_a_target_cannot_run(tmp_path: Path) -> None:
    project, module = _module(CORE, "helper")
    assert module.test_target is None
    with pytest.raises(RunError, match="has no integration target"):
        prepare_copy(project, module, Config(), tmp_path / "work")


# --- the command --------------------------------------------------------------------


def test_command_runs_in_docker_by_default() -> None:
    assert build_command("ansible-test", "thing", docker_image="default") == [
        "ansible-test",
        "integration",
        "thing",
        "--docker",
        "default",
    ]


def test_command_can_run_locally() -> None:
    assert build_command("/x/ansible-test", "thing", docker_image=None) == [
        "/x/ansible-test",
        "integration",
        "thing",
        "--local",
    ]


# --- reading the recording ----------------------------------------------------------


def test_recording_becomes_observations(tmp_path: Path) -> None:
    project, _ = _module(_collection(tmp_path), "thing")
    path = _write(
        tmp_path / "rec.jsonl",
        [_record(), _record(state="changed", keys=[{"path": "name", "type": "str"}])],
    )
    observations = {o.path: o for o in load_recording(path, project, Config())}
    assert set(observations) == {"name", "info", "info.size"}
    name = observations["name"]
    assert name.count == 2
    assert name.types == frozenset({"str"})
    assert name.results == frozenset({ResultState.OK, ResultState.CHANGED})
    assert name.sample == Sample("hello")
    target = project.targets_dir / "thing" / "tasks" / "main.yml"
    assert name.sources == (Location(target, 1),)
    assert observations["info"].sample is None  # a dict with keys has no value
    assert observations["info.size"].sample == Sample(3)


def test_recorded_samples_are_redacted_again(tmp_path: Path) -> None:
    project, _ = _module(_collection(tmp_path), "thing")
    record = _record(
        keys=[
            {"path": "pin", "type": "str", "value": "1234"},
            {"path": "conn", "type": "dict", "value": {"pin": "1234", "user": "u"}},
            {"path": "text", "type": "str", "value": "x" * 500},
        ]
    )
    path = _write(tmp_path / "rec.jsonl", [record])
    observations = {
        o.path: o
        for o in load_recording(path, project, Config(redact_patterns=("pin",)))
    }
    assert observations["pin"].sample is None
    assert observations["conn"].sample == Sample({"pin": "<redacted>", "user": "u"})
    text = observations["text"].sample
    assert text is not None
    assert isinstance(text.value, str)
    assert len(text.value) < 130


def test_locations_outside_the_target_are_dropped(tmp_path: Path) -> None:
    project, _ = _module(_collection(tmp_path), "thing")
    path = _write(
        tmp_path / "rec.jsonl",
        [
            _record(location={"file": "/elsewhere/play.yml", "line": 3}),
            _record(location=None),
        ],
    )
    (name, *_) = [
        o for o in load_recording(path, project, Config()) if o.path == "name"
    ]
    assert name.sources == ()
    assert name.count == 2


def test_failed_results_and_bad_lines(tmp_path: Path) -> None:
    project, _ = _module(_collection(tmp_path), "thing")
    path = _write(
        tmp_path / "rec.jsonl",
        [_record(state="failed"), "not json", json.dumps({"version": 99})],
    )
    observations = {o.path: o for o in load_recording(path, project, Config())}
    assert observations["name"].results == frozenset({ResultState.FAILED})


def test_a_missing_recording_has_no_observations(tmp_path: Path) -> None:
    project, _ = _module(_collection(tmp_path), "thing")
    assert load_recording(tmp_path / "absent.jsonl", project, Config()) == ()


# --- running ------------------------------------------------------------------------


class _FakeAnsibleTest:
    """Stands in for the ansible-test process: records what it ran, writes results."""

    def __init__(self, records: list[dict[str, Any]], returncode: int = 0) -> None:
        self.records = records
        self.returncode = returncode
        self.calls: list[tuple[list[str], Path]] = []

    def __call__(self, command: list[str], cwd: Path) -> int:
        self.calls.append((command, cwd))
        results = cwd / "tests" / "output" / "data" / RECORDING_NAME
        if self.records:
            _write(results, list(self.records))
        return self.returncode


def test_run_integration(tmp_path: Path) -> None:
    root = _collection(tmp_path)
    project, module = _module(root, "thing")
    fake = _FakeAnsibleTest([_record()])
    result = run_integration(
        project,
        module,
        Config(),
        docker_image="default",
        runner=fake,
        ansible_test="at",
    )
    ((command, cwd),) = fake.calls
    assert command == ["at", "integration", "thing", "--docker", "default"]
    assert cwd.name == "coll"
    assert cwd != root
    assert not cwd.exists()  # the copy is cleaned up
    assert {o.path for o in result.observations} == {"name", "info", "info.size"}
    assert result.returncode == 0
    assert result.records == 1


def test_run_integration_keeps_what_a_failing_run_recorded(tmp_path: Path) -> None:
    project, module = _module(_collection(tmp_path), "thing")
    fake = _FakeAnsibleTest([_record()], returncode=1)
    result = run_integration(
        project, module, Config(), docker_image=None, runner=fake, ansible_test="at"
    )
    assert result.returncode == 1
    assert result.records == 1


def test_run_integration_without_ansible_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, module = _module(_collection(tmp_path), "thing")
    monkeypatch.setattr(runtime, "find_ansible_test", lambda copy: None)
    with pytest.raises(RunError, match="ansible-test not found"):
        run_integration(project, module, Config(), docker_image="default")


def test_ansible_core_uses_its_own_ansible_test(tmp_path: Path) -> None:
    copy = tmp_path / "ansible"
    (copy / "bin").mkdir(parents=True)
    (copy / "bin" / "ansible-test").write_text("#!/bin/sh\n")
    assert runtime.find_ansible_test(copy) == str(copy / "bin" / "ansible-test")


# --- the CLI ------------------------------------------------------------------------


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch) -> _FakeAnsibleTest:
    fake = _FakeAnsibleTest([_record()])
    monkeypatch.setattr(runtime, "find_ansible_test", lambda copy: "at")
    monkeypatch.setattr(runtime, "default_runner", fake)
    return fake


def test_returns_run_adds_runtime_evidence(
    tmp_path: Path, fake_run: _FakeAnsibleTest
) -> None:
    root = _collection(tmp_path)
    result = CliRunner().invoke(
        main, ["returns", str(root), "thing", "--run", "--format", "json"]
    )
    assert result.exit_code == 1, result.output
    (module,) = json.loads(result.stdout)["modules"]
    keys = {k["name"]: k for k in module["keys"]}
    assert keys["info.size"]["observed"]["types"] == ["int"]
    assert keys["info.size"]["observed"]["sample"] == 3
    assert keys["name"]["observed"]["results"] == ["ok"]
    ((command, _),) = fake_run.calls
    assert command[-2:] == ["--docker", "default"]
    assert "recorded 1 result" in result.stderr


def test_draft_run_uses_runtime_samples(
    tmp_path: Path, fake_run: _FakeAnsibleTest
) -> None:
    root = _collection(tmp_path)
    result = CliRunner().invoke(main, ["draft", str(root), "thing", "--run", "--local"])
    assert result.exit_code == 0, result.output
    assert "sample: 3" in result.stdout
    ((command, _),) = fake_run.calls
    assert command[-1] == "--local"


def test_docker_image_option(tmp_path: Path, fake_run: _FakeAnsibleTest) -> None:
    root = _collection(tmp_path)
    CliRunner().invoke(
        main, ["returns", str(root), "thing", "--run", "--docker", "img"]
    )
    ((command, _),) = fake_run.calls
    assert command[-2:] == ["--docker", "img"]


def test_docker_image_from_config(tmp_path: Path, fake_run: _FakeAnsibleTest) -> None:
    root = _collection(tmp_path)
    (root / "dr-ansible.toml").write_text('docker-image = "fedora"\n')
    CliRunner().invoke(main, ["returns", str(root), "thing", "--run"])
    ((command, _),) = fake_run.calls
    assert command[-2:] == ["--docker", "fedora"]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--docker", "img"], "--docker needs --run"),
        (["--local"], "--local needs --run"),
        (["--run", "--local", "--docker", "img"], "--docker and --local"),
    ],
)
def test_run_option_errors(
    tmp_path: Path, fake_run: _FakeAnsibleTest, args: list[str], message: str
) -> None:
    root = _collection(tmp_path)
    for command in ("returns", "draft"):
        result = CliRunner().invoke(main, [command, str(root), "thing", *args])
        assert result.exit_code == 2
        assert message in result.stderr
    assert fake_run.calls == []


def test_nothing_runs_without_run(tmp_path: Path, fake_run: _FakeAnsibleTest) -> None:
    root = _collection(tmp_path)
    assert CliRunner().invoke(main, ["returns", str(root), "thing"]).exit_code == 1
    assert CliRunner().invoke(main, ["draft", str(root), "thing"]).exit_code == 0
    assert fake_run.calls == []


def test_run_without_a_target_is_an_error(fake_run: _FakeAnsibleTest) -> None:
    result = CliRunner().invoke(main, ["returns", str(CORE), "helper", "--run"])
    assert result.exit_code == 2
    assert "has no integration target" in result.stderr


def test_a_failed_run_that_recorded_nothing_is_an_error(
    tmp_path: Path, fake_run: _FakeAnsibleTest
) -> None:
    fake_run.records = []
    fake_run.returncode = 1
    root = _collection(tmp_path)
    result = CliRunner().invoke(main, ["returns", str(root), "thing", "--run"])
    assert result.exit_code == 2
    assert "ansible-test failed (exit code 1) and recorded nothing" in result.stderr


def test_a_failed_run_that_recorded_results_warns(
    tmp_path: Path, fake_run: _FakeAnsibleTest
) -> None:
    fake_run.returncode = 1
    root = _collection(tmp_path)
    result = CliRunner().invoke(main, ["returns", str(root), "thing", "--run"])
    assert result.exit_code == 1
    assert "ansible-test failed (exit code 1)" in result.stderr
    assert "using the 1 result it recorded" in result.stderr


def test_run_never_changes_the_project(
    tmp_path: Path, fake_run: _FakeAnsibleTest
) -> None:
    root = _collection(tmp_path)
    before = _digest(root)
    CliRunner().invoke(main, ["draft", str(root), "thing", "--run"])
    assert _digest(root) == before


# --- a real ansible-test run (local, no Docker) -------------------------------------


def test_real_local_run(tmp_path: Path) -> None:
    for program in ("ssh-keygen",):
        if shutil.which(program) is None:
            pytest.skip(f"ansible-test needs {program}")
    if runtime.find_ansible_test(tmp_path) is None:
        pytest.skip("ansible-test is not installed")
    root = _collection(tmp_path)
    before = _digest(root)
    project, module = _module(root, "thing")
    result = run_integration(project, module, Config(), docker_image=None)
    assert result.returncode == 0
    observations = {o.path: o for o in result.observations}
    assert observations["name"].types == frozenset({"str"})
    assert observations["name"].sample == Sample("hello")
    assert observations["info.size"].sample == Sample(3)
    target = project.targets_dir / "thing" / "tasks" / "main.yml"
    assert observations["name"].sources == (Location(target, 1),)
    assert _digest(root) == before
