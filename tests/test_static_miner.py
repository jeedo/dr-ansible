"""Tests for static mining of integration targets (plan task 16: FR-13, AC-5)."""

from pathlib import Path

import pytest

from dr_ansible.discovery import detect_project, discover_modules
from dr_ansible.mining.static_miner import MiningResult, Registration, mine_target
from dr_ansible.model import Language, Location, ModuleInfo

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CORE = FIXTURES / "core"
TARGETS = CORE / "test" / "integration" / "targets"


def _module(root: Path, name: str) -> ModuleInfo:
    return {m.name: m for m in discover_modules(detect_project(root))}[name]


def _line(path: Path, snippet: str) -> int:
    for number, text in enumerate(path.read_text().splitlines(), start=1):
        if snippet in text:
            return number
    raise AssertionError(f"{snippet!r} not in {path}")


def _counts(result: MiningResult) -> dict[str, int]:
    return {o.path: o.count for o in result.observations}


def _yaml_single(text: str) -> str:
    """Escape ``text`` for a single-quoted YAML scalar."""
    return text.replace("'", "''")


def _tmp_target(
    tmp_path: Path, files: dict[str, str], name: str = "thing"
) -> ModuleInfo:
    target = tmp_path / "targets" / name
    for relative, text in files.items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return ModuleInfo(
        name=name,
        fqcn=f"ns.coll.{name}",
        language=Language.PYTHON,
        module_path=tmp_path / f"{name}.py",
        aliases=(f"old_{name}",),
        test_target=target,
    )


# --- fixtures -----------------------------------------------------------------------


def test_incremental_target_under_every_module_name() -> None:
    main = TARGETS / "incremental" / "tasks" / "main.yml"
    result = mine_target(_module(CORE, "incremental"))
    assert result.registrations == (
        Registration(
            "inc_short",
            "incremental",
            Location(main, _line(main, "register: inc_short")),
        ),
        Registration(
            "inc_fqcn",
            "ansible.builtin.incremental",
            Location(main, _line(main, "register: inc_fqcn")),
        ),
        Registration(
            "inc_old",
            "ansible.builtin.old_incremental",
            Location(main, _line(main, "register: inc_old")),
        ),
    )
    assert _counts(result) == {
        "backup_file": 1,
        "checksum": 1,
        "mode": 1,
        "path": 1,
        "size": 1,
    }
    by_path = {o.path: o for o in result.observations}
    assert by_path["mode"].sources == (
        Location(main, _line(main, "inc_short['mode']")),
    )
    assert by_path["backup_file"].sources == (
        Location(main, _line(main, "var: inc_short.backup_file")),
    )
    assert result.problems == ()


def test_virtual_target_stops_at_method_calls_and_filters() -> None:
    result = mine_target(_module(CORE, "virtual"))
    assert [r.variable for r in result.registrations] == [
        "virtual_result",
        "virtual_missing",
    ]
    assert _counts(result) == {"checksum": 1, "dest": 1, "file": 1}


def test_roles_layout_and_failed_when() -> None:
    result = mine_target(_module(CORE, "hybrid"))
    assert _counts(result) == {"checksum": 1, "dest": 1, "transferred": 1}
    checksum = {o.path: o for o in result.observations}["checksum"]
    (source,) = checksum.sources
    assert source.file.parts[-4:] == ("roles", "check_hybrid", "tasks", "main.yml")


def test_keywords_target_sees_failure_keys() -> None:
    result = mine_target(_module(CORE, "keywords"))
    assert _counts(result) == {
        "count": 1,
        "enabled": 1,
        "ping": 1,
        "rc": 1,
        "stderr": 1,
    }


def test_collection_target() -> None:
    result = mine_target(_module(FIXTURES / "collection", "widget"))
    assert _counts(result) == {"name": 1, "widget_id": 1}


def test_module_without_a_target_has_no_evidence() -> None:
    assert mine_target(_module(CORE, "nested")) == MiningResult()


def test_static_observations_carry_no_runtime_detail() -> None:
    for observation in mine_target(_module(CORE, "incremental")).observations:
        assert observation.types == frozenset()
        assert observation.results == frozenset()
        assert observation.sample is None
        assert observation.count == len(observation.sources)


# --- reference syntax ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("r.rc == 0", {"rc": 1}),
        ("r['rc'] == 0", {"rc": 1}),
        ('r["rc"] == 0', {"rc": 1}),
        ("r.get('rc') == 0", {"rc": 1}),
        ("r.stat.exists", {"stat": 1, "stat.exists": 1}),
        ("r['stat']['size'] > 0", {"stat": 1, "stat.size": 1}),
        ("r.stdout.startswith('x')", {"stdout": 1}),
        ("r.stdout_lines | length == 2", {"stdout_lines": 1}),
        ("r.results[0].item == 'a'", {"results": 1}),
        ("r is changed", {}),
        ("r is defined and r.rc is defined", {"rc": 1}),
        ("r.rc == 0 and r.rc != 1", {"rc": 2}),
        ("other.rc == 0", {}),
        ("r2.rc == 0", {}),
        ("myr.rc == 0", {}),
        ("x.r.rc == 0", {}),
    ],
)
def test_reference_syntax(
    tmp_path: Path, expression: str, expected: dict[str, int]
) -> None:
    module = _tmp_target(
        tmp_path,
        {
            "tasks/main.yml": (
                "- thing:\n  register: r\n"
                f"- assert:\n    that:\n      - '{_yaml_single(expression)}'\n"
            )
        },
    )
    assert _counts(mine_target(module)) == expected


# --- fields and task forms ----------------------------------------------------------


def test_every_listed_field_is_read(tmp_path: Path) -> None:
    module = _tmp_target(
        tmp_path,
        {
            "tasks/main.yml": """\
- name: call
  ns.coll.thing:
  register: r
  failed_when: r.a is defined
  changed_when: r.b is defined
  until: r.c is defined
- debug:
    var: r.d
- ansible.builtin.debug:
    msg: "{{ r.e }} and {{ r['f'] }}"
- assert:
    that: r.g == 1
    fail_msg: "bad {{ r.h }}"
- command: echo
  when:
    - r.i
    - r.j
- name: other fields are not evidence
  copy:
    content: "{{ r.not_read }}"
""",
        },
    )
    assert sorted(_counts(mine_target(module))) == list("abcdefghij")


def test_action_and_local_action_forms(tmp_path: Path) -> None:
    module = _tmp_target(
        tmp_path,
        {
            "tasks/main.yml": """\
- action: thing arg=1
  register: a1
- local_action:
    module: old_thing
  register: a2
- action: something_else
  register: a3
- assert:
    that: [a1.x, a2.y, a3.z]
""",
        },
    )
    result = mine_target(module)
    assert [r.variable for r in result.registrations] == ["a1", "a2"]
    assert _counts(result) == {"x": 1, "y": 1}


def test_blocks_and_playbooks(tmp_path: Path) -> None:
    module = _tmp_target(
        tmp_path,
        {
            "runme.yml": """\
- hosts: all
  tasks:
    - block:
        - thing:
          register: in_block
      rescue:
        - debug:
            var: in_block.err
""",
        },
    )
    assert _counts(mine_target(module)) == {"err": 1}


def test_registrations_in_one_file_count_in_another(tmp_path: Path) -> None:
    module = _tmp_target(
        tmp_path,
        {
            "tasks/main.yml": "- thing:\n  register: r\n- include_tasks: check.yml\n",
            "tasks/check.yml": "- assert:\n    that: r.k == 1\n",
        },
    )
    assert _counts(mine_target(module)) == {"k": 1}


def test_unregistered_and_other_module_tasks_are_ignored(tmp_path: Path) -> None:
    module = _tmp_target(
        tmp_path,
        {
            "tasks/main.yml": (
                "- thing:\n- other:\n  register: o\n- assert:\n    that: o.k == 1\n"
            )
        },
    )
    result = mine_target(module)
    assert result.registrations == ()
    assert result.observations == ()


def test_line_numbers_inside_block_scalars(tmp_path: Path) -> None:
    module = _tmp_target(
        tmp_path,
        {
            "tasks/main.yml": (
                "- thing:\n"
                "  register: r\n"
                "- debug:\n"
                "    msg: |\n"
                "      first {{ r.one }}\n"
                "      second {{ r.two }}\n"
            )
        },
    )
    path = module.test_target / "tasks" / "main.yml"  # type: ignore[operator]
    by_path = {o.path: o for o in mine_target(module).observations}
    assert by_path["one"].sources == (Location(path, 5),)
    assert by_path["two"].sources == (Location(path, 6),)


# --- robustness ---------------------------------------------------------------------


def test_invalid_yaml_is_a_problem_not_a_crash(tmp_path: Path) -> None:
    module = _tmp_target(
        tmp_path,
        {
            "tasks/main.yml": "- thing:\n  register: r\n- assert:\n    that: r.ok\n",
            "tasks/broken.yml": "- [unclosed\n",
        },
    )
    result = mine_target(module)
    assert _counts(result) == {"ok": 1}
    (problem,) = result.problems
    assert "broken.yml" in problem


def test_ansible_tags_do_not_break_parsing(tmp_path: Path) -> None:
    module = _tmp_target(
        tmp_path,
        {
            "tasks/main.yml": (
                "- thing:\n    secret: !vault |\n      $ANSIBLE_VAULT;1.1;AES256\n"
                "  register: r\n"
                "- assert:\n    that: !unsafe r.k\n"
            )
        },
    )
    assert _counts(mine_target(module)) == {"k": 1}


def test_non_yaml_files_are_ignored(tmp_path: Path) -> None:
    module = _tmp_target(
        tmp_path,
        {
            "tasks/main.yml": "- thing:\n  register: r\n",
            "files/data.txt": "r.k == 1",
            "aliases": "shippable/posix/group1\n",
        },
    )
    assert mine_target(module).observations == ()


def test_results_are_deterministic() -> None:
    module = _module(CORE, "incremental")
    assert mine_target(module) == mine_target(module)
