"""Tests for the core data model (plan task 5)."""

import dataclasses
import json
from pathlib import Path

import pytest

from dr_ansible.model import (
    DOC_STATUSES,
    DocResult,
    DocumentedKey,
    KeyReport,
    KeyStatus,
    Language,
    Location,
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

MOD = Path("lib/ansible/modules/fetch.py")
ACTION = Path("lib/ansible/plugins/action/fetch.py")


def _info(**overrides: object) -> ModuleInfo:
    fields: dict[str, object] = {
        "name": "fetch",
        "fqcn": "ansible.builtin.fetch",
        "language": Language.PYTHON,
        "module_path": MOD,
    }
    fields.update(overrides)
    return ModuleInfo(**fields)  # type: ignore[arg-type]


def _static(path: str = "dest", line: int = 199) -> StaticKey:
    return StaticKey(
        path=path,
        file=ACTION,
        line=line,
        inferred_type=ReturnType.STR,
        outcome=Outcome.SUCCESS,
    )


def _documented(path: str = "dest") -> DocumentedKey:
    return DocumentedKey(
        path=path, type="str", returned="success", has_description=True
    )


def _observed(path: str = "dest") -> Observation:
    return Observation(
        path=path,
        count=1,
        types=frozenset({"str"}),
        results=frozenset({ResultState.CHANGED}),
        sources=(Location(Path("tasks/main.yml"), 3),),
    )


# --- enums -----------------------------------------------------------------


def test_status_strings_match_the_requirements() -> None:
    assert [s.value for s in ReturnStatus] == [
        "missing",
        "placeholder",
        "invalid",
        "present",
        "exempt",
        "error",
        "unsupported",
    ]
    assert [s.value for s in KeyStatus] == ["ok", "undocumented", "stale", "test-only"]
    assert {s.value for s in ResultState} == {"ok", "changed", "failed"}
    assert {o.value for o in Outcome} == {"success", "failure"}
    assert {lang.value for lang in Language} == {"python", "powershell"}


def test_return_types_match_the_validate_modules_schema() -> None:
    assert {t.value for t in ReturnType} == {
        "bool",
        "complex",
        "dict",
        "float",
        "int",
        "list",
        "raw",
        "str",
    }


def test_enums_serialise_as_plain_strings() -> None:
    assert json.dumps([KeyStatus.TEST_ONLY, ReturnStatus.MISSING]) == (
        '["test-only", "missing"]'
    )


def test_doc_statuses_exclude_report_only_statuses() -> None:
    assert ReturnStatus.ERROR not in DOC_STATUSES
    assert ReturnStatus.UNSUPPORTED not in DOC_STATUSES
    assert ReturnStatus.EXEMPT in DOC_STATUSES


# --- immutability ------------------------------------------------------------


@pytest.mark.parametrize(
    "obj",
    [
        _info(),
        _static(),
        _documented(),
        _observed(),
        Unresolved(file=MOD, line=10, reason="computed key"),
        Location(MOD, 1),
    ],
    ids=lambda o: type(o).__name__,
)
def test_models_are_frozen(obj: object) -> None:
    field = dataclasses.fields(obj)[0].name  # type: ignore[arg-type]
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(obj, field, None)


def test_collections_are_normalised_to_immutable_sorted_tuples() -> None:
    info = _info(aliases=["fetch_alias_b", "fetch_alias_a"])
    assert info.aliases == ("fetch_alias_a", "fetch_alias_b")


# --- ModuleInfo ----------------------------------------------------------------


def test_module_info_optional_paths_default_to_none() -> None:
    info = _info()
    assert info.sidecar_path is None
    assert info.action_path is None
    assert info.test_target is None
    assert info.aliases == ()


def test_module_info_paths_lists_only_known_locations() -> None:
    info = _info(action_path=ACTION)
    assert info.paths() == {"module": MOD, "action": ACTION}


@pytest.mark.parametrize("field", ["name", "fqcn"])
def test_module_info_rejects_empty_names(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        _info(**{field: ""})


# --- locations -----------------------------------------------------------------


@pytest.mark.parametrize("line", [0, -1])
def test_line_numbers_are_one_based(line: int) -> None:
    with pytest.raises(ValueError, match="line"):
        Location(MOD, line)
    with pytest.raises(ValueError, match="line"):
        Unresolved(file=MOD, line=line, reason="x")
    with pytest.raises(ValueError, match="line"):
        _static(line=line)


def test_locations_sort_by_file_then_line() -> None:
    a, b = Path("a.py"), Path("b.py")
    locs = [Location(b, 5), Location(a, 9), Location(b, 2)]
    assert sorted(locs) == [Location(a, 9), Location(b, 2), Location(b, 5)]


def test_unresolved_requires_a_reason() -> None:
    with pytest.raises(ValueError, match="reason"):
        Unresolved(file=MOD, line=1, reason="")


# --- keys ----------------------------------------------------------------------


@pytest.mark.parametrize("path", ["", ".dest", "dest.", "a..b"])
def test_key_paths_must_be_dotted_names(path: str) -> None:
    with pytest.raises(ValueError, match="path"):
        _documented(path)


def test_nested_key_path_parts() -> None:
    assert _documented("stat.exists").parts == ("stat", "exists")


def test_static_key_condition_is_optional() -> None:
    key = dataclasses.replace(_static(), condition="changed")
    assert key.condition == "changed"
    assert _static().condition is None


# --- Observation ---------------------------------------------------------------


def test_observation_count_is_positive() -> None:
    with pytest.raises(ValueError, match="count"):
        dataclasses.replace(_observed(), count=0)


def test_observation_sources_are_sorted() -> None:
    obs = dataclasses.replace(
        _observed(),
        sources=(Location(Path("b.yml"), 1), Location(Path("a.yml"), 7)),
    )
    assert obs.sources == (Location(Path("a.yml"), 7), Location(Path("b.yml"), 1))


def test_sample_distinguishes_null_from_absent() -> None:
    assert _observed().sample is None
    null_sample = dataclasses.replace(_observed(), sample=Sample(None))
    assert null_sample.sample == Sample(None)


# --- DocResult -----------------------------------------------------------------


def test_doc_result_keys_are_sorted_and_addressable_by_path() -> None:
    doc = DocResult(
        status=ReturnStatus.PRESENT,
        keys=(_documented("stat.exists"), _documented("stat")),
    )
    assert [k.path for k in doc.keys] == ["stat", "stat.exists"]
    assert doc.key("stat.exists") == _documented("stat.exists")
    assert doc.key("nope") is None


def test_doc_result_rejects_duplicate_paths() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        DocResult(status=ReturnStatus.PRESENT, keys=(_documented(), _documented()))


@pytest.mark.parametrize("status", [ReturnStatus.ERROR, ReturnStatus.UNSUPPORTED])
def test_doc_result_rejects_report_only_statuses(status: ReturnStatus) -> None:
    with pytest.raises(ValueError, match="status"):
        DocResult(status=status)


@pytest.mark.parametrize(
    "status", [ReturnStatus.MISSING, ReturnStatus.PLACEHOLDER, ReturnStatus.INVALID]
)
def test_only_present_or_exempt_docs_have_keys(status: ReturnStatus) -> None:
    with pytest.raises(ValueError, match="keys"):
        DocResult(status=status, keys=(_documented(),))


# --- KeyReport -----------------------------------------------------------------


def test_ok_key_is_documented_and_found() -> None:
    report = KeyReport(
        name="dest",
        status=KeyStatus.OK,
        documented=_documented(),
        static=(_static(),),
    )
    assert report.status is KeyStatus.OK


@pytest.mark.parametrize(
    ("status", "documented", "static", "observed"),
    [
        (KeyStatus.OK, False, True, False),  # ok needs documentation
        (KeyStatus.OK, True, False, False),  # ok needs evidence
        (KeyStatus.UNDOCUMENTED, True, True, False),
        (KeyStatus.UNDOCUMENTED, False, False, False),
        (KeyStatus.STALE, True, True, False),
        (KeyStatus.STALE, True, False, True),
        (KeyStatus.STALE, False, False, False),
        (KeyStatus.TEST_ONLY, False, True, True),
        (KeyStatus.TEST_ONLY, False, False, False),
    ],
)
def test_key_status_must_match_its_evidence(
    status: KeyStatus, documented: bool, static: bool, observed: bool
) -> None:
    with pytest.raises(ValueError, match="status"):
        KeyReport(
            name="dest",
            status=status,
            documented=_documented() if documented else None,
            static=(_static(),) if static else (),
            observed=_observed() if observed else None,
        )


def test_key_report_evidence_must_be_for_the_same_key() -> None:
    with pytest.raises(ValueError, match="path"):
        KeyReport(name="dest", status=KeyStatus.UNDOCUMENTED, static=(_static("file"),))


def test_key_report_static_evidence_is_sorted_by_location() -> None:
    report = KeyReport(
        name="dest",
        status=KeyStatus.UNDOCUMENTED,
        static=(_static(line=208), _static(line=199)),
    )
    assert [s.line for s in report.static] == [199, 208]


# --- ModuleReport --------------------------------------------------------------


def _key(name: str) -> KeyReport:
    return KeyReport(name=name, status=KeyStatus.UNDOCUMENTED, static=(_static(name),))


def test_module_report_sorts_keys_and_unresolved() -> None:
    report = ModuleReport(
        module=_info(),
        return_status=ReturnStatus.MISSING,
        keys=(_key("file"), _key("dest")),
        unresolved=(
            Unresolved(file=MOD, line=20, reason="b"),
            Unresolved(file=MOD, line=3, reason="a"),
        ),
    )
    assert [k.name for k in report.keys] == ["dest", "file"]
    assert [u.line for u in report.unresolved] == [3, 20]
    assert report.fqcn == "ansible.builtin.fetch"


def test_module_report_rejects_duplicate_keys() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        ModuleReport(
            module=_info(),
            return_status=ReturnStatus.MISSING,
            keys=(_key("dest"), _key("dest")),
        )


def test_error_status_requires_a_reason_and_vice_versa() -> None:
    with pytest.raises(ValueError, match="error"):
        ModuleReport(module=_info(), return_status=ReturnStatus.ERROR)
    with pytest.raises(ValueError, match="error"):
        ModuleReport(module=_info(), return_status=ReturnStatus.MISSING, error="boom")
    report = ModuleReport(
        module=_info(), return_status=ReturnStatus.ERROR, error="SyntaxError: line 3"
    )
    assert report.error == "SyntaxError: line 3"


def test_powershell_modules_are_unsupported() -> None:
    ps1 = _info(language=Language.POWERSHELL, module_path=Path("win_ping.ps1"))
    with pytest.raises(ValueError, match="unsupported"):
        ModuleReport(module=ps1, return_status=ReturnStatus.MISSING)
    with pytest.raises(ValueError, match="unsupported"):
        ModuleReport(module=_info(), return_status=ReturnStatus.UNSUPPORTED)
    report = ModuleReport(module=ps1, return_status=ReturnStatus.UNSUPPORTED)
    assert report.keys == ()


def test_inherits_from_names_another_module() -> None:
    report = ModuleReport(
        module=_info(name="copy", fqcn="ansible.builtin.copy"),
        return_status=ReturnStatus.PRESENT,
        inherits_from="ansible.builtin.copy",
    )
    assert report.inherits_from == "ansible.builtin.copy"
