"""Tests for reconciling evidence into statuses (plan task 18: FR-15, FR-17, FR-18)."""

from pathlib import Path

import pytest

from dr_ansible.config import Config
from dr_ansible.discovery import detect_project, discover_modules
from dr_ansible.docs_audit import audit_docs
from dr_ansible.mining.static_miner import mine_target
from dr_ansible.model import (
    DocResult,
    DocumentedKey,
    KeyStatus,
    Language,
    Location,
    ModuleInfo,
    ModuleReport,
    Observation,
    Outcome,
    ResultState,
    ReturnStatus,
    Sample,
    StaticKey,
    Unresolved,
)
from dr_ansible.reconcile import build_report, merge_observations, reconcile_keys
from dr_ansible.static.action_analyzer import analyze_action
from dr_ansible.static.module_analyzer import analyze_module

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CORE = FIXTURES / "core"
SRC = Path("module.py")


def _doc(path: str) -> DocumentedKey:
    return DocumentedKey(path=path, type="str", returned="always", has_description=True)


def _static(path: str, line: int = 1) -> StaticKey:
    return StaticKey(path=path, file=SRC, line=line, outcome=Outcome.SUCCESS)


def _obs(path: str, **kwargs: object) -> Observation:
    fields: dict[str, object] = {"path": path, "count": 1}
    fields.update(kwargs)
    return Observation(**fields)  # type: ignore[arg-type]


def _statuses(**kwargs: object) -> dict[str, KeyStatus]:
    reports = reconcile_keys(**kwargs)  # type: ignore[arg-type]
    return {r.name: r.status for r in reports}


# --- the status matrix (FR-17) ------------------------------------------------------


@pytest.mark.parametrize(
    ("documented", "static", "observed", "status"),
    [
        (True, True, False, KeyStatus.OK),
        (True, True, True, KeyStatus.OK),
        (True, False, True, KeyStatus.OK),  # decision: tests confirm the docs
        (False, True, False, KeyStatus.UNDOCUMENTED),
        (False, True, True, KeyStatus.UNDOCUMENTED),
        (False, False, True, KeyStatus.TEST_ONLY),
        (True, False, False, KeyStatus.STALE),
    ],
)
def test_status_matrix(
    documented: bool, static: bool, observed: bool, status: KeyStatus
) -> None:
    statuses = _statuses(
        documented=[_doc("k")] if documented else [],
        static=[_static("k")] if static else [],
        observations=[_obs("k")] if observed else [],
        config=Config(),
    )
    assert statuses == {"k": status}


def test_each_key_report_carries_all_its_evidence() -> None:
    (report,) = reconcile_keys(
        documented=[_doc("k")],
        static=[_static("k", 9), _static("k", 3)],
        observations=[_obs("k", count=2)],
        config=Config(),
    )
    assert report.documented == _doc("k")
    assert [s.line for s in report.static] == [3, 9]
    assert report.observed is not None
    assert report.observed.count == 2


def test_reports_are_sorted_by_key() -> None:
    reports = reconcile_keys(
        documented=[_doc("zeta")],
        static=[_static("alpha"), _static("mid.child"), _static("mid")],
        observations=[_obs("beta")],
        config=Config(),
    )
    assert [r.name for r in reports] == ["alpha", "beta", "mid", "mid.child", "zeta"]


def test_no_evidence_gives_no_reports() -> None:
    assert (
        reconcile_keys(documented=[], static=[], observations=[], config=Config()) == ()
    )


# --- common return values (FR-18) ---------------------------------------------------


def test_common_keys_are_excluded_by_default_from_every_source() -> None:
    statuses = _statuses(
        documented=[_doc("msg"), _doc("dest")],
        static=[_static("changed"), _static("dest"), _static("diff.before")],
        observations=[_obs("failed"), _obs("rc")],
        config=Config(),
    )
    assert statuses == {"dest": KeyStatus.OK, "rc": KeyStatus.TEST_ONLY}


def test_include_common_keeps_them() -> None:
    statuses = _statuses(
        documented=[],
        static=[_static("changed"), _static("diff.before")],
        observations=[],
        config=Config(),
        include_common=True,
    )
    assert statuses == {
        "changed": KeyStatus.UNDOCUMENTED,
        "diff.before": KeyStatus.UNDOCUMENTED,
    }


def test_common_keys_come_from_config() -> None:
    config = Config(common_return_keys=frozenset({"rc"}))
    statuses = _statuses(
        documented=[],
        static=[_static("rc"), _static("changed")],
        observations=[],
        config=config,
    )
    assert statuses == {"changed": KeyStatus.UNDOCUMENTED}


def test_only_the_top_level_name_decides_commonness() -> None:
    statuses = _statuses(
        documented=[],
        static=[_static("result.changed"), _static("message")],
        observations=[],
        config=Config(),
    )
    assert statuses == {
        "message": KeyStatus.UNDOCUMENTED,
        "result.changed": KeyStatus.UNDOCUMENTED,
    }


# --- merging observations (FR-15) ---------------------------------------------------


def test_observations_of_one_key_are_combined() -> None:
    a, b = Path("a.yml"), Path("b.yml")
    merged = merge_observations(
        [
            _obs(
                "k",
                count=2,
                types=frozenset({"str"}),
                results=frozenset({ResultState.OK}),
                sources=(Location(b, 1), Location(a, 9)),
            ),
            _obs(
                "k",
                count=1,
                types=frozenset({"int"}),
                results=frozenset({ResultState.CHANGED}),
                sample=Sample("first"),
                sources=(Location(a, 2),),
            ),
            _obs("k", count=4, sample=Sample("second")),
            _obs("other", count=1),
        ]
    )
    k, other = merged
    assert k.path == "k"
    assert k.count == 7
    assert k.types == frozenset({"str", "int"})
    assert k.results == frozenset({ResultState.OK, ResultState.CHANGED})
    assert k.sample == Sample("first")  # first sample in order (NFR-6)
    assert k.sources == (Location(a, 2), Location(a, 9), Location(b, 1))
    assert other.count == 1


def test_a_null_sample_counts_as_a_sample() -> None:
    (merged,) = merge_observations(
        [_obs("k", sample=Sample(None)), _obs("k", sample=Sample("later"))]
    )
    assert merged.sample == Sample(None)


def test_reconcile_merges_observations_itself() -> None:
    (report,) = reconcile_keys(
        documented=[],
        static=[],
        observations=[_obs("k", count=1), _obs("k", count=2)],
        config=Config(),
    )
    assert report.observed is not None
    assert report.observed.count == 3


# --- build_report -------------------------------------------------------------------


def _info() -> ModuleInfo:
    return ModuleInfo(
        name="thing", fqcn="ns.coll.thing", language=Language.PYTHON, module_path=SRC
    )


def test_build_report_assembles_everything() -> None:
    unresolved = Unresolved(file=SRC, line=4, reason="computed key name")
    report = build_report(
        module=_info(),
        docs=DocResult(status=ReturnStatus.PRESENT, keys=(_doc("a"), _doc("gone"))),
        static=[_static("a"), _static("b")],
        unresolved=[unresolved],
        inherits_from=["ns.coll.other"],
        observations=[_obs("c")],
        config=Config(),
    )
    assert report.return_status is ReturnStatus.PRESENT
    assert {k.name: k.status for k in report.keys} == {
        "a": KeyStatus.OK,
        "b": KeyStatus.UNDOCUMENTED,
        "c": KeyStatus.TEST_ONLY,
        "gone": KeyStatus.STALE,
    }
    assert report.unresolved == (unresolved,)
    assert report.inherits_from == ("ns.coll.other",)
    assert report.fqcn == "ns.coll.thing"


def test_inherited_modules_add_no_keys() -> None:
    report = build_report(
        module=_info(),
        docs=DocResult(status=ReturnStatus.MISSING),
        static=[],
        unresolved=[],
        inherits_from=["ansible.legacy.slurp"],
        observations=[],
        config=Config(),
    )
    assert report.keys == ()
    assert report.inherits_from == ("ansible.legacy.slurp",)


# --- end to end over the fixtures ---------------------------------------------------


def _fixture_report(
    name: str, root: Path = CORE, include_common: bool = False
) -> ModuleReport:
    module = {m.name: m for m in discover_modules(detect_project(root))}[name]
    config = Config()
    docs = audit_docs(module, config)
    module_analysis = analyze_module(module.module_path)
    static = list(module_analysis.keys)
    unresolved = list(module_analysis.unresolved)
    inherits: list[str] = []
    if module.action_path is not None:
        action = analyze_action(module.action_path, module.fqcn)
        static += action.keys
        unresolved += action.unresolved
        inherits += action.inherits_from
    return build_report(
        module=module,
        docs=docs,
        static=static,
        unresolved=unresolved,
        inherits_from=inherits,
        observations=mine_target(module).observations,
        config=config,
        include_common=include_common,
    )


def _fixture_statuses(name: str, root: Path = CORE) -> dict[str, KeyStatus]:
    return {k.name: k.status for k in _fixture_report(name, root).keys}


def test_incremental_everything_is_undocumented() -> None:
    assert _fixture_statuses("incremental") == {
        "backup_file": KeyStatus.UNDOCUMENTED,
        "checksum": KeyStatus.UNDOCUMENTED,
        "mode": KeyStatus.UNDOCUMENTED,
        "owner": KeyStatus.UNDOCUMENTED,
        "path": KeyStatus.UNDOCUMENTED,
        "size": KeyStatus.UNDOCUMENTED,
    }


def test_helper_mixes_ok_undocumented_and_stale() -> None:
    assert _fixture_statuses("helper") == {
        "legacy_id": KeyStatus.STALE,
        "name": KeyStatus.OK,
        "owner": KeyStatus.UNDOCUMENTED,
        "state": KeyStatus.OK,
    }


def test_nested_documentation_agrees_with_the_code() -> None:
    assert _fixture_statuses("nested") == {
        "info": KeyStatus.OK,
        "info.exists": KeyStatus.OK,
        "info.owner": KeyStatus.OK,
        "info.size": KeyStatus.OK,
    }


def test_virtual_module_keys_come_from_the_action_plugin() -> None:
    assert _fixture_statuses("virtual") == {
        "checksum": KeyStatus.UNDOCUMENTED,
        "dest": KeyStatus.UNDOCUMENTED,
        "file": KeyStatus.UNDOCUMENTED,
        "remote_checksum": KeyStatus.UNDOCUMENTED,
        "src": KeyStatus.UNDOCUMENTED,
    }


def test_hybrid_reports_inheritance_and_both_sources() -> None:
    report = _fixture_report("hybrid")
    assert report.inherits_from == ("ansible.builtin.hybrid",)
    assert {k.name: k.status for k in report.keys} == {
        "checksum": KeyStatus.OK,
        "dest": KeyStatus.OK,
        "transferred": KeyStatus.UNDOCUMENTED,
    }


def test_keywords_failure_only_keys_are_undocumented_with_failure_outcome() -> None:
    report = _fixture_report("keywords")
    assert report.return_status is ReturnStatus.PLACEHOLDER
    keys = {k.name: k for k in report.keys}
    assert {s.outcome for s in keys["rc"].static} == {Outcome.FAILURE}
    assert keys["rc"].observed is not None  # also read by the integration test
    assert keys["ping"].status is KeyStatus.UNDOCUMENTED


def test_dynamic_carries_its_unresolved_entry() -> None:
    report = _fixture_report("dynamic")
    assert [u.reason for u in report.unresolved] == ["computed key name"]
    assert [k.name for k in report.keys] == ["status"]


def test_include_common_end_to_end() -> None:
    names = {k.name for k in _fixture_report("virtual", include_common=True).keys}
    assert {"changed", "failed", "msg"} <= names


def test_collection_module() -> None:
    assert _fixture_statuses("widget", FIXTURES / "collection") == {
        "name": KeyStatus.UNDOCUMENTED,
        "widget_id": KeyStatus.OK,
    }
