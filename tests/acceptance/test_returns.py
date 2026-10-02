"""``returns`` on the pinned ansible-core checkout: AC-4 to AC-7 (plan task 31)."""

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from dr_ansible.cli import main
from dr_ansible.discovery import detect_project, discover_modules
from dr_ansible.mining.static_miner import mine_target

pytestmark = pytest.mark.acceptance

FETCH_ACTION = "lib/ansible/plugins/action/fetch.py"
FETCH_TARGET = "test/integration/targets/fetch/roles/fetch_tests/tasks"


def _returns(root: Path, module: str, *args: str) -> dict[str, Any]:
    result = CliRunner().invoke(
        main, ["returns", str(root), module, "--format", "json", *args]
    )
    (data,) = json.loads(result.stdout)["modules"]
    assert isinstance(data, dict)
    return data


def _keys(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {k["name"]: k for k in data["keys"]}


def _code(key: dict[str, Any]) -> set[tuple[str, int]]:
    return {(s["file"], s["line"]) for s in key["static"]}


# --- AC-4: fetch's keys and line numbers --------------------------------------------


#: Where fetch's action plugin sets each key, checked against the source.
FETCH_KEYS = {
    "checksum": {197, 200, 208},
    "dest": {196, 199, 208},
    "file": {84, 118, 196, 199, 208},
    "md5sum": {195, 199, 208},
    "remote_checksum": {197, 201},
    "remote_md5sum": {196, 200},
}


def test_ac4_fetch_keys_and_lines(ansible_core: Path) -> None:
    data = _returns(ansible_core, "fetch")
    keys = _keys(data)
    assert set(keys) == set(FETCH_KEYS)
    for name, lines in FETCH_KEYS.items():
        assert _code(keys[name]) == {(FETCH_ACTION, line) for line in lines}, name
        assert keys[name]["status"] == "undocumented"
    assert data["return_status"] == "missing"
    assert data["unresolved"] == []


def test_ac4_line_numbers_point_at_the_keys(ansible_core: Path) -> None:
    source = (ansible_core / FETCH_ACTION).read_text().splitlines()
    for name, lines in FETCH_KEYS.items():
        for line in lines:
            text = source[line - 1]
            assert f"{name}=" in text or f"'{name}'" in text, (name, line, text)


def test_ac4_common_keys_only_with_include_common(ansible_core: Path) -> None:
    assert not {"changed", "failed", "msg"} & set(
        _keys(_returns(ansible_core, "fetch"))
    )
    common = _keys(_returns(ansible_core, "fetch", "--include-common"))
    assert {"changed", "failed", "msg"} <= set(common)
    assert set(FETCH_KEYS) <= set(common)


# --- AC-5: fetch's integration tests ------------------------------------------------


def test_ac5_registered_variables_link_to_observed_keys(ansible_core: Path) -> None:
    project = detect_project(ansible_core)
    fetch = {m.name: m for m in discover_modules(project)}["fetch"]
    mining = mine_target(fetch)
    assert mining.problems == ()
    registered = {
        (r.variable, r.location.file.name, r.location.line)
        for r in mining.registrations
    }
    assert ("fetch_missing_nofail", "fail_on_missing.yml", 6) in registered
    # fetch_missing_nofail.msg is read at fail_on_missing.yml:41.
    msg = {o.path: o for o in mining.observations}["msg"]
    sources = {(s.file.name, s.line) for s in msg.sources}
    assert ("fail_on_missing.yml", 41) in sources


def test_ac5_observed_keys_reach_the_report(ansible_core: Path) -> None:
    keys = _keys(_returns(ansible_core, "fetch", "--include-common"))
    sources = {(s["file"], s["line"]) for s in keys["msg"]["observed"]["sources"]}
    assert (f"{FETCH_TARGET}/fail_on_missing.yml", 41) in sources
    for name in ("checksum", "dest", "file", "remote_checksum"):
        assert keys[name]["observed"] is not None, name
    for name in ("md5sum", "remote_md5sum"):
        assert keys[name]["observed"] is None, name  # no test reads them


# --- AC-6: stat's nested keys -------------------------------------------------------


def test_ac6_stat_is_ok(ansible_core: Path) -> None:
    keys = _keys(_returns(ansible_core, "stat"))
    assert keys["stat"]["status"] == "ok"
    assert keys["stat"]["documented"]["type"] == "dict"


def test_ac6_every_documented_nested_key_agrees_with_the_code(
    ansible_core: Path,
) -> None:
    data = _returns(ansible_core, "stat")
    keys = _keys(data)
    documented = {n for n, k in keys.items() if k["documented"] is not None}
    assert len(documented) > 40
    assert {keys[n]["status"] for n in documented} == {"ok"}
    assert not [n for n, k in keys.items() if k["status"] == "stale"]
    assert data["unresolved"] == []
    # readable, writeable and executable are set in a loop over a literal list.
    for name in ("stat.readable", "stat.writeable", "stat.executable"):
        assert keys[name]["status"] == "ok"
        assert _code(keys[name]) == {("lib/ansible/modules/stat.py", 493)}


def test_ac6_keys_the_docs_lack_are_undocumented(ansible_core: Path) -> None:
    keys = _keys(_returns(ansible_core, "stat"))
    undocumented = {n for n, k in keys.items() if k["status"] == "undocumented"}
    # attr_flags is always set with get_attributes; the rest are platform-specific
    # stat fields copied under `if hasattr(st, ...)`.
    assert undocumented == {
        "stat.attr_flags",
        "stat.attrs",
        "stat.birthtime",
        "stat.block_size",
        "stat.blocks",
        "stat.creator",
        "stat.device_type",
        "stat.file_type",
        "stat.flags",
        "stat.generation",
        "stat.object_type",
        "stat.real_size",
    }
    blocks = keys["stat.blocks"]["static"]
    assert {s["condition"] for s in blocks} == {"hasattr(st, other[0])"}


# --- AC-7: copy inherits the copy module's returns ----------------------------------


def test_ac7_copy_inherits_from_the_copy_module(ansible_core: Path) -> None:
    data = _returns(ansible_core, "copy")
    assert "ansible.legacy.copy" in data["inherits_from"]
    assert data["paths"]["action"] == "lib/ansible/plugins/action/copy.py"
    assert data["paths"]["module"] == "lib/ansible/modules/copy.py"


def test_ac7_inheritance_is_shown_in_the_table(ansible_core: Path) -> None:
    result = CliRunner().invoke(main, ["returns", str(ansible_core), "copy"])
    assert "inherits returns from: ansible.legacy.copy" in result.stdout
