"""Tests for the runtime recorder callback plugin (plan task 26: FR-14, FR-16)."""

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dr_ansible.config import DEFAULT_REDACT_PATTERNS, Config
from dr_ansible.mining import redact
from dr_ansible.mining.callback import CALLBACK_DIR, CALLBACK_NAME
from dr_ansible.mining.callback import dr_ansible_recorder as recorder

# --- fakes standing in for ansible's CallbackTaskResult -----------------------------


class _Task:
    def __init__(
        self,
        action: str,
        *,
        resolved: str | None = None,
        no_log: bool = False,
        loop: object = None,
        path: str = "/t/tasks/main.yml:7",
    ) -> None:
        self.action = action
        self.resolved_action = resolved
        self.no_log = no_log
        self.loop = loop
        self.loop_with = None
        self._path = path

    def get_path(self) -> str:
        return self._path

    def get_name(self) -> str:
        return f"run {self.action}"


def _result(task: _Task, data: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(task=task, result=data)


def _callback(tmp_path: Path, names: list[str] | None = None, **kwargs: Any) -> Any:
    callback = recorder.CallbackModule()
    callback.configure(
        output=str(tmp_path / "out.jsonl"),
        module_names=names if names is not None else ["ping", "ansible.builtin.ping"],
        redact_patterns=list(kwargs.get("patterns", DEFAULT_REDACT_PATTERNS)),
    )
    return callback


def _records(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "out.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _keys(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {k["path"]: k for k in record["keys"]}


# --- recording ----------------------------------------------------------------------


def test_records_keys_types_and_values(tmp_path: Path) -> None:
    callback = _callback(tmp_path)
    callback.v2_runner_on_ok(_result(_Task("ping"), {"ping": "pong", "changed": False}))
    (record,) = _records(tmp_path)
    assert record["version"] == recorder.RECORD_VERSION == 1
    assert record["module"] == "ping"
    assert record["state"] == "ok"
    assert record["task"] == "run ping"
    assert record["location"] == {"file": "/t/tasks/main.yml", "line": 7}
    assert _keys(record) == {
        "changed": {"path": "changed", "type": "bool", "value": False},
        "ping": {"path": "ping", "type": "str", "value": "pong"},
    }


def test_result_states(tmp_path: Path) -> None:
    callback = _callback(tmp_path)
    callback.v2_runner_on_ok(_result(_Task("ping"), {"ping": "a", "changed": True}))
    callback.v2_runner_on_failed(_result(_Task("ping"), {"msg": "boom"}))
    callback.v2_runner_on_failed(
        _result(_Task("ping"), {"msg": "ignored"}), ignore_errors=True
    )
    assert [r["state"] for r in _records(tmp_path)] == ["changed", "failed", "failed"]


def test_nested_dicts_become_dotted_paths(tmp_path: Path) -> None:
    callback = _callback(tmp_path, ["stat"])
    callback.v2_runner_on_ok(
        _result(
            _Task("stat"),
            {"stat": {"exists": True, "size": 3, "mode": "0644", "x": None}},
        )
    )
    keys = _keys(_records(tmp_path)[0])
    assert {p: k["type"] for p, k in keys.items()} == {
        "stat": "dict",
        "stat.exists": "bool",
        "stat.mode": "str",
        "stat.size": "int",
        "stat.x": "NoneType",
    }
    assert keys["stat.size"]["value"] == 3


def test_lists_are_recorded_but_not_descended_into(tmp_path: Path) -> None:
    callback = _callback(tmp_path)
    callback.v2_runner_on_ok(
        _result(_Task("ping"), {"items": [{"a": 1}, 2.5], "pair": ("x", "y")})
    )
    keys = _keys(_records(tmp_path)[0])
    assert set(keys) == {"items", "pair"}
    assert keys["items"] == {"path": "items", "type": "list", "value": [{"a": 1}, 2.5]}
    assert keys["pair"]["type"] == "list"
    assert keys["pair"]["value"] == ["x", "y"]


def test_internal_and_unusable_keys_are_skipped(tmp_path: Path) -> None:
    callback = _callback(tmp_path)
    callback.v2_runner_on_ok(
        _result(
            _Task("ping"),
            {
                "_ansible_no_log": False,
                "_ansible_verbose_always": True,
                "ok": 1,
                "a.b": 2,
                "": 3,
                "nested": {"with.dot": 1, "fine": 2},
            },
        )
    )
    assert set(_keys(_records(tmp_path)[0])) == {"ok", "nested", "nested.fine"}


def test_types_of_str_subclasses_and_odd_values(tmp_path: Path) -> None:
    class Unsafe(str):
        pass

    callback = _callback(tmp_path)
    callback.v2_runner_on_ok(
        _result(
            _Task("ping"),
            {"text": Unsafe("hi"), "nan": float("nan"), "other": object()},
        )
    )
    keys = _keys(_records(tmp_path)[0])
    assert keys["text"] == {"path": "text", "type": "str", "value": "hi"}
    assert keys["nan"]["type"] == "float"
    assert keys["nan"]["value"] == "nan"  # JSON has no NaN
    assert keys["other"]["type"] == "object"
    assert keys["other"]["value"].startswith("<object object")


def test_only_the_module_under_test_is_recorded(tmp_path: Path) -> None:
    callback = _callback(tmp_path)
    callback.v2_runner_on_ok(_result(_Task("debug"), {"msg": "x"}))
    callback.v2_runner_on_ok(_result(_Task("ansible.builtin.ping"), {"ping": "a"}))
    callback.v2_runner_on_ok(
        _result(_Task("legacy_name", resolved="ansible.builtin.ping"), {"ping": "b"})
    )
    assert [r["module"] for r in _records(tmp_path)] == [
        "ansible.builtin.ping",
        "legacy_name",
    ]


def test_no_module_names_records_every_module(tmp_path: Path) -> None:
    callback = _callback(tmp_path, [])
    callback.v2_runner_on_ok(_result(_Task("debug"), {"msg": "x"}))
    assert len(_records(tmp_path)) == 1


def test_loop_items_are_recorded_one_by_one(tmp_path: Path) -> None:
    callback = _callback(tmp_path)
    task = _Task("ping", loop=["a", "b"])
    callback.v2_runner_item_on_ok(_result(task, {"ping": "a", "item": "a"}))
    callback.v2_runner_item_on_failed(_result(task, {"msg": "no", "item": "b"}))
    # The summary of the whole loop is not a module result.
    callback.v2_runner_on_ok(_result(task, {"results": [], "changed": False}))
    assert [r["state"] for r in _records(tmp_path)] == ["ok", "failed"]


def test_location_without_a_line(tmp_path: Path) -> None:
    callback = _callback(tmp_path)
    callback.v2_runner_on_ok(_result(_Task("ping", path=""), {"ping": "a"}))
    assert _records(tmp_path)[0]["location"] is None


# --- safety (FR-16, AC-12) ----------------------------------------------------------


def test_no_log_results_are_skipped(tmp_path: Path) -> None:
    callback = _callback(tmp_path)
    callback.v2_runner_on_ok(_result(_Task("ping", no_log=True), {"ping": "secret"}))
    callback.v2_runner_on_ok(
        _result(_Task("ping"), {"censored": "hidden", "_ansible_no_log": True})
    )
    assert _records(tmp_path) == []


def test_sensitive_keys_keep_their_type_but_lose_their_value(tmp_path: Path) -> None:
    callback = _callback(tmp_path)
    callback.v2_runner_on_ok(
        _result(
            _Task("ping"),
            {
                "api_token": "abc123",
                "conn": {"user": "u", "password": "hunter2"},
            },
        )
    )
    text = (tmp_path / "out.jsonl").read_text()
    assert "abc123" not in text
    assert "hunter2" not in text
    keys = _keys(_records(tmp_path)[0])
    assert keys["api_token"] == {"path": "api_token", "type": "str"}
    assert keys["conn.password"] == {"path": "conn.password", "type": "str"}
    # A dict with keys has no value of its own: its keys carry theirs.
    assert keys["conn"] == {"path": "conn", "type": "dict"}
    assert keys["conn.user"]["value"] == "u"


def test_sensitive_keys_inside_lists_are_redacted(tmp_path: Path) -> None:
    callback = _callback(tmp_path)
    callback.v2_runner_on_ok(
        _result(
            _Task("ping"), {"users": [{"name": "a", "password": "hunter2"}], "e": {}}
        )
    )
    keys = _keys(_records(tmp_path)[0])
    assert keys["users"]["value"] == [{"name": "a", "password": "<redacted>"}]
    assert keys["e"] == {"path": "e", "type": "dict", "value": {}}


def test_redaction_patterns_are_configurable(tmp_path: Path) -> None:
    callback = _callback(tmp_path, patterns=["^pin$"])
    callback.v2_runner_on_ok(_result(_Task("ping"), {"pin": "1234", "token": "t"}))
    keys = _keys(_records(tmp_path)[0])
    assert "value" not in keys["pin"]
    assert keys["token"]["value"] == "t"


def test_long_values_are_truncated(tmp_path: Path) -> None:
    callback = _callback(tmp_path)
    text = "x" * 500
    lines = "\n".join(f"line {n}" for n in range(10))
    callback.v2_runner_on_ok(_result(_Task("ping"), {"long": text, "content": lines}))
    keys = _keys(_records(tmp_path)[0])
    assert keys["long"]["value"] == redact.redact_value(text, Config())
    assert keys["content"]["value"] == redact.redact_value(lines, Config())


@pytest.mark.parametrize(
    "value",
    [
        "short",
        "y" * 121,
        "y" * 123,
        "y" * 124,
        "y" * 500,
        "a\nb\nc\nd",
        "a\nb\nc\n...",
        "\n".join("z" * 100 for _ in range(6)),
        {"token": "t", "nested": {"secret_key": 1, "ok": ["p" * 200]}},
        [{"password": "x"}, "q" * 130],
        None,
        3.5,
        True,
    ],
)
def test_redaction_matches_dr_ansible(value: Any) -> None:
    # The callback cannot import dr_ansible (it runs inside the ansible-test
    # container), so it carries its own copy of the rules: keep them in step.
    patterns = recorder.compile_patterns(DEFAULT_REDACT_PATTERNS)
    assert recorder.redact_value(value, patterns) == redact.redact_value(
        value, Config()
    )


def test_redaction_constants_match_dr_ansible() -> None:
    assert recorder.REDACTED == redact.REDACTED
    assert recorder.MAX_LENGTH == redact.MAX_LENGTH
    assert recorder.MAX_LINES == redact.MAX_LINES


def test_without_an_output_file_nothing_is_written(tmp_path: Path) -> None:
    callback = recorder.CallbackModule()
    callback.configure(output="", module_names=[], redact_patterns=[])
    assert callback.disabled
    callback.v2_runner_on_ok(_result(_Task("ping"), {"ping": "a"}))
    assert list(tmp_path.iterdir()) == []


def test_the_callback_is_self_contained() -> None:
    tree = ast.parse((CALLBACK_DIR / f"{CALLBACK_NAME}.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    # Only the standard library and ansible: dr_ansible is not installed where it runs.
    assert imported <= {*sys.stdlib_module_names, "ansible"}


def test_the_callback_ships_in_the_package() -> None:
    assert (CALLBACK_DIR / f"{CALLBACK_NAME}.py").is_file()
    assert recorder.CallbackModule.CALLBACK_NAME == CALLBACK_NAME
    assert recorder.CallbackModule.CALLBACK_NEEDS_ENABLED is True


# --- a real ansible-playbook run ----------------------------------------------------

PLAYBOOK = """\
- hosts: localhost
  gather_facts: false
  tasks:
    - name: plain ping
      ansible.builtin.ping:
    - name: ping by its short name
      ping:
        data: hello
    - name: ping in a loop
      ping:
        data: "{{ item }}"
      loop: [one, two]
    - name: hidden ping
      ping:
        data: very-private-value
      no_log: true
    - name: failing ping
      ping:
        data: crash
      ignore_errors: true
    - name: not the module under test
      ansible.builtin.debug:
        msg: ignore me
"""


def test_real_playbook_run(tmp_path: Path) -> None:
    playbook_bin = shutil.which("ansible-playbook")
    if playbook_bin is None:
        pytest.skip("ansible-playbook is not installed")
    (tmp_path / "play.yml").write_text(PLAYBOOK)
    output = tmp_path / "recording.jsonl"
    env = {
        **os.environ,
        "ANSIBLE_CALLBACK_PLUGINS": str(CALLBACK_DIR),
        "ANSIBLE_CALLBACKS_ENABLED": CALLBACK_NAME,
        "ANSIBLE_LOCALHOST_WARNING": "false",
        "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false",
        "ANSIBLE_LOCAL_TEMP": str(tmp_path / "tmp"),
        "ANSIBLE_HOME": str(tmp_path / "home"),
        "DR_ANSIBLE_OUTPUT": str(output),
        "DR_ANSIBLE_MODULE_NAMES": "ping,ansible.builtin.ping,ansible.legacy.ping",
    }
    proc = subprocess.run(
        [playbook_bin, "-i", "localhost,", "-c", "local", str(tmp_path / "play.yml")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    records = [json.loads(line) for line in output.read_text().splitlines()]
    assert [(r["task"], r["state"]) for r in records] == [
        ("plain ping", "ok"),
        ("ping by its short name", "ok"),
        ("ping in a loop", "ok"),
        ("ping in a loop", "ok"),
        ("failing ping", "failed"),
    ]
    first = _keys(records[0])
    assert first["ping"] == {"path": "ping", "type": "str", "value": "pong"}  # AC-11
    assert _keys(records[1])["ping"]["value"] == "hello"
    assert [_keys(r)["ping"]["value"] for r in records[2:4]] == ["one", "two"]
    assert records[0]["location"]["file"] == str(tmp_path / "play.yml")
    assert records[0]["location"]["line"] == 4
    assert "very-private-value" not in output.read_text()  # AC-12
