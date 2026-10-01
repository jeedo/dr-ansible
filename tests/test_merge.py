"""Tests for merge-mode drafts (plan task 20: FR-20, AC-9)."""

import re
from pathlib import Path
from typing import Any

import yaml

from dr_ansible.config import Config
from dr_ansible.discovery import detect_project, discover_modules
from dr_ansible.docs_audit import audit_docs
from dr_ansible.draft import MARKER, render_draft, render_merge
from dr_ansible.mining.static_miner import mine_target
from dr_ansible.model import DocResult, Language, ModuleInfo, ModuleReport
from dr_ansible.reconcile import build_report
from dr_ansible.static.action_analyzer import analyze_action
from dr_ansible.static.module_analyzer import analyze_module

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CORE = FIXTURES / "core"


def _pieces(module: ModuleInfo, root: Path) -> tuple[ModuleReport, DocResult]:
    config = Config()
    docs = audit_docs(module, config)
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
        docs=docs,
        static=static,
        unresolved=unresolved,
        inherits_from=inherits,
        observations=mine_target(module).observations,
        config=config,
    )
    return report, docs


def _fixture(name: str, root: Path = CORE) -> tuple[str, ModuleReport, DocResult]:
    project = detect_project(root)
    module = {m.name: m for m in discover_modules(project)}[name]
    report, docs = _pieces(module, project.root)
    return render_merge(report, docs, Config(), project.root), report, docs


def _tmp(tmp_path: Path, source: str) -> tuple[str, ModuleReport, DocResult]:
    path = tmp_path / "thing.py"
    path.write_text(source)
    module = ModuleInfo(
        name="thing", fqcn="ns.coll.thing", language=Language.PYTHON, module_path=path
    )
    report, docs = _pieces(module, tmp_path)
    return render_merge(report, docs, Config(), tmp_path), report, docs


def _return_body(draft: str) -> str:
    match = re.search(r'^RETURN = r"""(.*)"""$', draft, flags=re.S | re.M)
    assert match is not None, draft
    return match.group(1)


def _parse(draft: str) -> dict[str, Any]:
    data = yaml.safe_load(_return_body(draft))
    assert isinstance(data, dict)
    return data


# --- merging into an existing RETURN (AC-9) -----------------------------------------


def test_existing_entries_are_kept_byte_for_byte_and_missing_keys_appended() -> None:
    draft, _, docs = _fixture("helper")
    assert docs.raw_text is not None
    body = _return_body(draft)
    assert body.startswith(docs.raw_text)
    added = body[len(docs.raw_text) :]
    assert "owner:" in added
    assert "name:" not in added
    assert "state:" not in added
    entries = _parse(draft)
    assert set(entries) == {"name", "state", "legacy_id", "owner"}
    assert entries["owner"]["description"] == MARKER
    assert entries["name"]["description"] == "The name that was looked up."


def test_stale_entries_are_never_removed() -> None:
    draft, _, _ = _fixture("helper")
    assert "legacy_id" in _parse(draft)


def test_appended_entries_carry_evidence_comments() -> None:
    draft, _, docs = _fixture("helper")
    added = _return_body(draft)[len(docs.raw_text or "") :]
    assert re.search(r"    # from lib/ansible/modules/helper.py:\d+", added)


def test_merging_into_a_placeholder() -> None:
    draft, _, docs = _fixture("keywords")
    assert docs.raw_text == "#"
    body = _return_body(draft)
    assert body.startswith("#\n")
    entries = _parse(draft)
    assert entries["rc"]["returned"] == "failure"
    assert entries["ping"]["description"] == MARKER


def test_nothing_missing_leaves_the_block_unchanged() -> None:
    draft, _, docs = _fixture("nested")
    assert _return_body(draft) == docs.raw_text
    assert "# nothing to add" in draft


def test_odd_formatting_survives_exactly(tmp_path: Path) -> None:
    existing = (
        "\n# a comment the author wrote\n"
        "kept:   \n"
        "    description: 'single quoted'  \n"
        "    returned: always\n"
        "    type: str\n"
    )
    source = (
        f'RETURN = r"""{existing}"""\ndef main():\n    m.exit_json(kept=1, new=2)\n'
    )
    draft, _, _ = _tmp(tmp_path, source)
    body = _return_body(draft)
    assert body.startswith(existing)
    assert set(_parse(draft)) == {"kept", "new"}


def test_text_without_a_trailing_newline_gets_one_before_new_keys(
    tmp_path: Path,
) -> None:
    existing = "\nkept:\n    description: x\n    returned: always\n    type: str"
    source = (
        f'RETURN = r"""{existing}"""\ndef main():\n    m.exit_json(kept=1, new=2)\n'
    )
    draft, _, _ = _tmp(tmp_path, source)
    body = _return_body(draft)
    assert body.startswith(existing + "\n")
    assert set(_parse(draft)) == {"kept", "new"}


# --- nested keys under existing entries ---------------------------------------------


NESTED_DOC = '''RETURN = r"""
info:
    description: Facts.
    returned: success
    type: complex
    contains:
        exists:
            description: Whether it exists.
            type: bool
"""
'''


def test_missing_nested_keys_are_listed_as_comments(tmp_path: Path) -> None:
    source = NESTED_DOC + (
        "def main():\n"
        "    info = {'exists': True}\n"
        "    info['size'] = 3\n"
        "    info['meta'] = {'owner': 'root'}\n"
        "    m.exit_json(info=info)\n"
    )
    draft, _, docs = _tmp(tmp_path, source)
    body = _return_body(draft)
    assert body.startswith(docs.raw_text or "")
    comments = [line for line in body.splitlines() if line.startswith("#   info.")]
    offset = NESTED_DOC.count("\n")  # the code starts after the RETURN block
    assert comments == [
        f"#   info.meta: type complex, returned success; from thing.py:{offset + 4}",
        f"#   info.meta.owner: type str, returned success; from thing.py:{offset + 4}",
        f"#   info.size: type int, returned success; from thing.py:{offset + 3}",
    ]
    # The comments do not change what the YAML says.
    assert _parse(draft) == yaml.safe_load(docs.raw_text or "")


def test_missing_top_level_and_nested_keys_together(tmp_path: Path) -> None:
    source = NESTED_DOC + (
        "def main():\n    m.exit_json(info={'exists': True, 'size': 3}, extra='x')\n"
    )
    draft, _, _ = _tmp(tmp_path, source)
    entries = _parse(draft)
    assert set(entries) == {"info", "extra"}
    assert "#   info.size:" in draft


# --- when merging is not possible ---------------------------------------------------


def test_missing_return_gives_the_full_draft() -> None:
    draft, report, _ = _fixture("incremental")
    full = render_draft(report, Config(), CORE)
    assert "# no existing RETURN to merge into: this is a full draft" in draft
    assert draft.endswith(full.split("\n", 1)[1])  # same body as the full draft


def test_invalid_return_gives_the_full_draft_with_a_warning(tmp_path: Path) -> None:
    source = 'RETURN = r"""\nx: [unclosed\n"""\ndef main():\n    m.exit_json(a=1)\n'
    draft, _, _ = _tmp(tmp_path, source)
    assert "# the existing RETURN is not valid YAML" in draft
    assert set(_parse(draft)) == {"a"}


def test_sidecar_docs_get_a_snippet_to_paste_under_return() -> None:
    draft, _, _ = _fixture("sidecar")
    assert (
        "# add these entries under RETURN: in lib/ansible/modules/sidecar.yml" in draft
    )
    assert "RETURN = r" not in draft


def test_sidecar_snippet_is_valid_under_the_return_key(tmp_path: Path) -> None:
    (tmp_path / "thing.py").write_text(
        "def main():\n    m.exit_json(version='1', new=2)\n"
    )
    (tmp_path / "thing.yml").write_text(
        "RETURN:\n  version:\n    description: v\n    returned: always\n    type: str\n"
    )
    module = ModuleInfo(
        name="thing",
        fqcn="ns.coll.thing",
        language=Language.PYTHON,
        module_path=tmp_path / "thing.py",
        sidecar_path=tmp_path / "thing.yml",
    )
    report, docs = _pieces(module, tmp_path)
    draft = render_merge(report, docs, Config(), tmp_path)
    snippet = "\n".join(line for line in draft.splitlines() if not line.startswith("#"))
    merged = yaml.safe_load((tmp_path / "thing.yml").read_text() + snippet + "\n")
    assert set(merged["RETURN"]) == {"version", "new"}


def test_sidecar_without_a_return_key_says_so(tmp_path: Path) -> None:
    (tmp_path / "thing.py").write_text("def main():\n    m.exit_json(new=2)\n")
    (tmp_path / "thing.yml").write_text("DOCUMENTATION:\n  module: thing\n")
    module = ModuleInfo(
        name="thing",
        fqcn="ns.coll.thing",
        language=Language.PYTHON,
        module_path=tmp_path / "thing.py",
        sidecar_path=tmp_path / "thing.yml",
    )
    report, docs = _pieces(module, tmp_path)
    draft = render_merge(report, docs, Config(), tmp_path)
    assert "# thing.yml has no RETURN: key; add one with these entries" in draft
    assert yaml.safe_load("RETURN:\n" + draft)["RETURN"]["new"]["description"] == MARKER


def test_merge_is_deterministic() -> None:
    first, _, _ = _fixture("helper")
    second, _, _ = _fixture("helper")
    assert first == second
