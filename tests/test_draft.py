"""Tests for the RETURN draft generator (plan task 19: FR-19, FR-21)."""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from dr_ansible.config import Config
from dr_ansible.discovery import detect_project, discover_modules
from dr_ansible.docs_audit import audit_docs
from dr_ansible.draft import MARKER, DraftEntry, build_entries, render_draft
from dr_ansible.mining.static_miner import mine_target
from dr_ansible.model import (
    DocumentedKey,
    KeyReport,
    KeyStatus,
    Language,
    ModuleInfo,
    ModuleReport,
    Observation,
    Outcome,
    ResultState,
    ReturnStatus,
    ReturnType,
    Sample,
    StaticKey,
    Unresolved,
)
from dr_ansible.reconcile import build_report
from dr_ansible.static.action_analyzer import analyze_action
from dr_ansible.static.module_analyzer import analyze_module

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CORE = FIXTURES / "core"
ROOT = Path("/repo")
SRC = ROOT / "plugins" / "modules" / "thing.py"
VALID_TYPES = {t.value for t in ReturnType}
T = ReturnType


def _static(
    path: str,
    *,
    line: int = 1,
    outcome: Outcome = Outcome.SUCCESS,
    type_: ReturnType | None = None,
    condition: str | None = None,
    literal: object = None,
    file: Path = SRC,
) -> StaticKey:
    return StaticKey(
        path=path,
        file=file,
        line=line,
        outcome=outcome,
        inferred_type=type_,
        condition=condition,
        literal=None if literal is None else Sample(literal),  # type: ignore[arg-type]
    )


def _key(
    name: str,
    *static: StaticKey,
    observed: Observation | None = None,
    documented: DocumentedKey | None = None,
) -> KeyReport:
    if documented is not None:
        status = KeyStatus.OK if static or observed else KeyStatus.STALE
    else:
        status = KeyStatus.UNDOCUMENTED if static else KeyStatus.TEST_ONLY
    return KeyReport(
        name=name,
        status=status,
        documented=documented,
        static=static,
        observed=observed,
    )


def _entry(*keys: KeyReport) -> DraftEntry:
    (entry,) = build_entries(keys, Config())
    return entry


def _parse(draft: str) -> dict[str, Any]:
    match = re.search(r'^RETURN = r"""\n(.*)"""$', draft, flags=re.S | re.M)
    assert match is not None, draft
    data = yaml.safe_load(match.group(1))
    assert isinstance(data, dict)
    return data


def _check_schema(entries: dict[str, Any], *, top_level: bool = True) -> None:
    """The validate-modules return schema, as far as a skeleton needs it."""
    for name, entry in entries.items():
        assert entry["description"] == MARKER, name
        assert entry["type"] in VALID_TYPES, name
        if top_level:
            assert isinstance(entry["returned"], str), name
        assert set(entry) <= {
            "description",
            "returned",
            "type",
            "elements",
            "sample",
            "contains",
        }, name
        if "elements" in entry:
            assert entry["type"] == "list", name
            assert entry["elements"] in VALID_TYPES, name
        if "contains" in entry:
            assert entry["type"] == "complex", name
            _check_schema(entry["contains"], top_level=False)
        else:
            assert entry["type"] != "complex", name


# --- returned -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("statics", "returned"),
    [
        ([_static("k")], "success"),
        ([_static("k", outcome=Outcome.FAILURE)], "failure"),
        ([_static("k"), _static("k", line=2, outcome=Outcome.FAILURE)], "always"),
        (
            [
                _static("k", condition="x"),
                _static("k", line=2, outcome=Outcome.FAILURE),
            ],
            "success",
        ),
        ([_static("k", condition="changed")], "changed"),
        ([_static("k", condition="state == 'present'")], "when state == 'present'"),
        (
            [_static("k", condition="a"), _static("k", line=2, condition="a")],
            "when a",
        ),
        ([_static("k", condition="a"), _static("k", line=2, condition="b")], "success"),
        ([_static("k", condition="a"), _static("k", line=2)], "success"),
    ],
)
def test_returned_from_static_evidence(statics: list[StaticKey], returned: str) -> None:
    assert _entry(_key("k", *statics)).returned == returned


@pytest.mark.parametrize(
    ("results", "returned"),
    [
        (frozenset({ResultState.FAILED}), "failure"),
        (frozenset({ResultState.CHANGED}), "changed"),
        (frozenset({ResultState.OK, ResultState.CHANGED}), "success"),
        (frozenset(), "success"),
    ],
)
def test_returned_from_observed_results(
    results: frozenset[ResultState], returned: str
) -> None:
    observed = Observation(path="k", count=1, results=results)
    assert _entry(_key("k", observed=observed)).returned == returned


# --- type and elements --------------------------------------------------------------


@pytest.mark.parametrize(
    ("statics", "observed_types", "documented_type", "expected"),
    [
        ([_static("k", type_=T.INT)], set(), None, "int"),
        ([_static("k", type_=T.INT)], {"str"}, None, "str"),  # runtime wins
        (
            [_static("k", type_=T.INT), _static("k", line=2, type_=T.FLOAT)],
            set(),
            None,
            "float",
        ),
        (
            [_static("k", type_=T.INT), _static("k", line=2, type_=T.STR)],
            set(),
            None,
            "raw",
        ),
        ([_static("k")], {"int", "float"}, None, "float"),
        ([_static("k")], {"str", "NoneType"}, None, "str"),
        ([_static("k")], {"str", "dict"}, None, "raw"),
        ([_static("k")], set(), "bool", "bool"),
        ([_static("k")], set(), "complex", "dict"),
        ([_static("k")], set(), "nonsense", "raw"),
        ([_static("k")], set(), None, "raw"),
        ([_static("k", type_=T.DICT)], set(), None, "dict"),
    ],
)
def test_type_choice(
    statics: list[StaticKey],
    observed_types: set[str],
    documented_type: str | None,
    expected: str,
) -> None:
    observed = (
        Observation(path="k", count=1, types=frozenset(observed_types))
        if observed_types
        else None
    )
    documented = (
        DocumentedKey(path="k", type=documented_type) if documented_type else None
    )
    entry = _entry(_key("k", *statics, observed=observed, documented=documented))
    assert entry.type == expected


def test_nested_keys_make_a_complex_entry_with_contains() -> None:
    entries = build_entries(
        [
            _key("info", _static("info", type_=T.DICT)),
            _key("info.size", _static("info.size", type_=T.INT, literal=1)),
            _key("info.deep", _static("info.deep", type_=T.DICT)),
            _key("info.deep.x", _static("info.deep.x", type_=T.STR)),
        ],
        Config(),
    )
    (info,) = entries
    assert info.type == "complex"
    assert [c.name for c in info.contains] == ["deep", "size"]
    deep = info.contains[0]
    assert deep.type == "complex"
    assert [c.path for c in deep.contains] == ["info.deep.x"]


def test_missing_parents_are_created() -> None:
    (parent,) = build_entries([_key("a.b", _static("a.b", type_=T.STR))], Config())
    assert parent.name == "a"
    assert parent.type == "complex"
    assert parent.returned == "success"
    assert parent.sources == ()
    assert [c.name for c in parent.contains] == ["b"]


@pytest.mark.parametrize(
    ("sample", "documented_elements", "elements"),
    [
        (["a", "b"], None, "str"),
        ([1, 2], None, "int"),
        ([1, "a"], None, None),
        ([], None, None),
        ([], "dict", "dict"),
        ([1, 2.5], None, "float"),
    ],
)
def test_list_elements(
    sample: list[object], documented_elements: str | None, elements: str | None
) -> None:
    documented = (
        DocumentedKey(path="k", elements=documented_elements)
        if documented_elements
        else None
    )
    entry = _entry(
        _key("k", _static("k", type_=T.LIST, literal=sample), documented=documented)
    )
    assert entry.type == "list"
    assert entry.elements == elements


# --- sample -------------------------------------------------------------------------


def test_sample_prefers_tests_then_literal() -> None:
    observed = Observation(path="k", count=1, sample=Sample("from-test"))
    entry = _entry(_key("k", _static("k", literal="from-code"), observed=observed))
    assert entry.sample == Sample("from-test")
    assert _entry(_key("k", _static("k", literal="from-code"))).sample == Sample(
        "from-code"
    )
    assert _entry(_key("k", _static("k"))).sample is None


def test_first_literal_in_line_order() -> None:
    entry = _entry(
        _key(
            "k",
            _static("k", line=9, literal="late"),
            _static("k", line=2, literal="early"),
        )
    )
    assert entry.sample == Sample("early")


def test_samples_are_redacted() -> None:
    secret = _entry(_key("api_token", _static("api_token", literal="s3cr3t")))
    assert secret.sample is None
    nested = _entry(
        _key(
            "conn",
            _static("conn", type_=T.DICT, literal={"user": "u", "password": "p"}),
        )
    )
    assert nested.sample == Sample({"user": "u", "password": "<redacted>"})


def test_complex_entries_have_no_sample() -> None:
    (info,) = build_entries(
        [
            _key("info", _static("info", type_=T.DICT, literal={"size": 1})),
            _key("info.size", _static("info.size", literal=1)),
        ],
        Config(),
    )
    assert info.sample is None
    assert info.contains[0].sample == Sample(1)


# --- which keys ---------------------------------------------------------------------


def test_stale_keys_are_left_out_and_order_is_alphabetical() -> None:
    entries = build_entries(
        [
            _key("zeta", _static("zeta")),
            _key("gone", documented=DocumentedKey(path="gone")),
            _key("alpha", observed=Observation(path="alpha", count=1)),
            _key("kept", _static("kept"), documented=DocumentedKey(path="kept")),
        ],
        Config(),
    )
    assert [e.name for e in entries] == ["alpha", "kept", "zeta"]


# --- rendering ----------------------------------------------------------------------


def _report(*keys: KeyReport, unresolved: tuple[Unresolved, ...] = ()) -> ModuleReport:
    module = ModuleInfo(
        name="thing",
        fqcn="ns.coll.thing",
        language=Language.PYTHON,
        module_path=SRC,
    )
    return ModuleReport(
        module=module,
        return_status=ReturnStatus.MISSING,
        keys=keys,
        unresolved=unresolved,
    )


def test_rendered_draft_layout() -> None:
    action = ROOT / "plugins" / "action" / "thing.py"
    report = _report(
        _key(
            "dest",
            _static("dest", line=199, type_=T.STR, file=action),
            _static("dest", line=208, type_=T.STR, file=action),
            observed=Observation(path="dest", count=4, sample=Sample("/tmp/x")),
        ),
    )
    assert render_draft(report, Config(), ROOT) == (
        "# dr-ansible draft for ns.coll.thing: replace every DR-ANSIBLE-TODO with a"
        " description.\n"
        'RETURN = r"""\n'
        "dest:\n"
        "    # from plugins/action/thing.py:199,208; seen in 4 tests\n"
        "    description: DR-ANSIBLE-TODO\n"
        "    returned: success\n"
        "    type: str\n"
        "    sample: /tmp/x\n"
        '"""\n'
    )


def test_evidence_comments() -> None:
    other = ROOT / "plugins" / "module_utils" / "x.py"
    report = _report(
        _key(
            "a",
            _static("a", line=3),
            _static("a", line=1),
            _static("a", line=7, file=other),
        ),
        _key("b", observed=Observation(path="b", count=1)),
    )
    draft = render_draft(report, Config(), ROOT)
    # Files in path order, lines in numeric order.
    assert (
        "    # from plugins/module_utils/x.py:7 and plugins/modules/thing.py:1,3\n"
        in draft
    )
    assert "    # seen in 1 test\n" in draft


def test_paths_outside_the_root_stay_absolute() -> None:
    report = _report(_key("a", _static("a", file=Path("/elsewhere/x.py"))))
    assert "# from /elsewhere/x.py:1\n" in render_draft(report, Config(), ROOT)


def test_unresolved_items_are_listed_in_the_header() -> None:
    report = _report(
        _key("a", _static("a")),
        unresolved=(Unresolved(file=SRC, line=12, reason="computed key name"),),
    )
    draft = render_draft(report, Config(), ROOT)
    assert "# unresolved: plugins/modules/thing.py:12: computed key name\n" in draft


@pytest.mark.parametrize(
    ("value", "rendered"),
    [
        ("plain", "plain"),
        ("/tmp/x", "/tmp/x"),
        ("0644", '"0644"'),
        ("yes", '"yes"'),
        ("", '""'),
        ("a: b", '"a: b"'),
        ("two\nlines", '"two\\nlines"'),
        (" padded", '" padded"'),
        (3, "3"),
        (0.5, "0.5"),
        (True, "true"),
        (None, "null"),
        (["a", 1], '["a", 1]'),
        ({"k": "v"}, '{"k": "v"}'),
    ],
)
def test_sample_rendering_round_trips(value: object, rendered: str) -> None:
    report = _report(_key("k", _static("k", literal=value)))
    draft = render_draft(report, Config(), ROOT)
    if value is None:
        assert "sample:" not in draft  # None is never a literal sample
        return
    assert f"    sample: {rendered}\n" in draft
    assert _parse(draft)["k"]["sample"] == value


def test_returned_conditions_are_quoted_when_needed() -> None:
    report = _report(_key("k", _static("k", condition="task_vars.get('validate')")))
    draft = render_draft(report, Config(), ROOT)
    assert _parse(draft)["k"]["returned"] == "when task_vars.get('validate')"


def test_odd_key_names_are_quoted() -> None:
    report = _report(_key("yes", _static("yes")), _key("200", _static("200")))
    assert set(_parse(render_draft(report, Config(), ROOT))) == {"yes", "200"}


def test_empty_draft() -> None:
    draft = render_draft(_report(), Config(), ROOT)
    assert "# no return keys were found" in draft
    assert 'RETURN = r"""#"""' in draft


def test_rendering_is_deterministic() -> None:
    report = _report(_key("b", _static("b")), _key("a", _static("a")))
    assert render_draft(report, Config(), ROOT) == render_draft(report, Config(), ROOT)


def test_only_the_marker_is_ever_a_description() -> None:
    report = _report(
        _key(
            "k",
            _static("k"),
            documented=DocumentedKey(path="k", has_description=True),
        )
    )
    assert re.findall(r"description: (.*)", render_draft(report, Config(), ROOT)) == [
        MARKER
    ]


# --- end to end over the fixtures ---------------------------------------------------


def _fixture_draft(name: str, root: Path = CORE) -> str:
    project = detect_project(root)
    module = {m.name: m for m in discover_modules(project)}[name]
    config = Config()
    analysis = analyze_module(module.module_path)
    static = list(analysis.keys)
    unresolved = list(analysis.unresolved)
    inherits: list[str] = []
    if module.action_path is not None:
        action = analyze_action(module.action_path, module.fqcn)
        static += action.keys
        unresolved += action.unresolved
        inherits += action.inherits_from
    report = build_report(
        module=module,
        docs=audit_docs(module, config),
        static=static,
        unresolved=unresolved,
        inherits_from=inherits,
        observations=mine_target(module).observations,
        config=config,
    )
    return render_draft(report, config, project.root)


@pytest.mark.parametrize(
    "name",
    ["dynamic", "helper", "hybrid", "incremental", "keywords", "nested", "virtual"],
)
def test_fixture_drafts_follow_the_schema(name: str) -> None:
    _check_schema(_parse(_fixture_draft(name)))


def test_incremental_draft() -> None:
    entries = _parse(_fixture_draft("incremental"))
    assert entries["backup_file"]["returned"] == "changed"
    assert entries["size"] == {
        "description": MARKER,
        "returned": "success",
        "type": "int",
        "sample": 42,
    }
    assert entries["mode"]["sample"] == "0644"
    assert entries["path"]["type"] == "raw"
    assert "sample" not in entries["path"]
    assert "changed" not in entries  # a common return value


def test_keywords_draft_includes_failure_only_keys() -> None:
    entries = _parse(_fixture_draft("keywords"))
    assert entries["rc"]["returned"] == "failure"
    assert entries["rc"]["type"] == "int"
    assert entries["items"]["elements"] == "str"
    assert entries["items"]["sample"] == ["a", "b"]


def test_nested_draft_uses_contains() -> None:
    info = _parse(_fixture_draft("nested"))["info"]
    assert info["type"] == "complex"
    assert info["contains"]["size"]["sample"] == 1024
    assert info["contains"]["exists"]["type"] == "bool"


def test_virtual_draft_comes_from_the_action_plugin() -> None:
    draft = _fixture_draft("virtual")
    entries = _parse(draft)
    assert entries["remote_checksum"]["returned"] == "when task_vars.get('validate')"
    assert "# from lib/ansible/plugins/action/virtual.py:" in draft
    assert "seen in 1 test" in draft


def test_dynamic_draft_lists_the_unresolved_key() -> None:
    draft = _fixture_draft("dynamic")
    assert re.search(
        r"# unresolved: lib/ansible/modules/dynamic.py:\d+: computed key name", draft
    )


def test_drafts_never_contain_redacted_secrets(tmp_path: Path) -> None:
    source = (
        "def main():\n"
        "    m.exit_json(password='hunter2', creds={'token': 'abc123', 'user': 'u'})\n"
    )
    path = tmp_path / "secretive.py"
    path.write_text(source)
    module = ModuleInfo(
        name="secretive",
        fqcn="ns.coll.secretive",
        language=Language.PYTHON,
        module_path=path,
    )
    analysis = analyze_module(path)
    report = build_report(
        module=module,
        docs=audit_docs(module, Config()),
        static=analysis.keys,
        unresolved=analysis.unresolved,
        inherits_from=(),
        observations=(),
        config=Config(),
    )
    draft = render_draft(report, Config(), tmp_path)
    assert "hunter2" not in draft
    assert "abc123" not in draft
    assert _parse(draft)["password"]["type"] == "str"
