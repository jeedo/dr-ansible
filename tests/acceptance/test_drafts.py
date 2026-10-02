"""AC-8: every AC-1 draft passes validate-modules once pasted in (plan task 32).

The pinned checkout is copied, the ``dr-ansible draft`` output for each module
missing ``RETURN`` is pasted into the copy, and ansible-test's own
``sanity --test validate-modules`` runs on those files. The cached checkout is
never modified. ``--venv`` lets ansible-test install its sanity requirements
(it needs network access the first time); no container is needed.
"""

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from dr_ansible.cli import main
from dr_ansible.draft import MARKER
from tests.acceptance.checkout import MARKER as PIN_MARKER
from tests.acceptance.paste import paste_draft
from tests.acceptance.test_audit import MISSING

pytestmark = pytest.mark.acceptance

PYTHON = f"{sys.version_info.major}.{sys.version_info.minor}"


def _modules_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((root / "lib" / "ansible" / "modules").rglob("*.py")):
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _copy(checkout: Path, into: Path) -> Path:
    copy = into / "ansible"
    shutil.copytree(
        checkout,
        copy,
        symlinks=True,
        ignore=shutil.ignore_patterns(PIN_MARKER, "results", "__pycache__"),
    )
    return copy


def _module(root: Path, name: str) -> Path:
    return root / "lib" / "ansible" / "modules" / f"{name}.py"


def _paste(checkout: Path, copy: Path, names: set[str]) -> dict[str, str]:
    """Paste each module's draft (made from the checkout) into the copy."""
    drafts = {}
    for name in sorted(names):
        result = CliRunner().invoke(main, ["draft", str(checkout), name])
        assert result.exit_code == 0, result.output
        drafts[name] = result.stdout
        path = _module(copy, name)
        path.write_text(paste_draft(path.read_text(), result.stdout))
    return drafts


def _validate(copy: Path, names: set[str]) -> subprocess.CompletedProcess[str]:
    files = [str(_module(copy, n).relative_to(copy)) for n in sorted(names)]
    return subprocess.run(
        [
            sys.executable,
            str(copy / "bin" / "ansible-test"),
            "sanity",
            "--test",
            "validate-modules",
            "--venv",
            "--python",
            PYTHON,
            "--color",
            "no",
            *files,
        ],
        cwd=copy,
        capture_output=True,
        text=True,
        check=False,
        timeout=1800,
    )


@pytest.fixture(scope="module")
def pasted(
    ansible_core: Path, tmp_path_factory: pytest.TempPathFactory
) -> tuple[Path, dict[str, str]]:
    before = _modules_digest(ansible_core)
    copy = _copy(ansible_core, tmp_path_factory.mktemp("ac8"))
    drafts = _paste(ansible_core, copy, MISSING)
    assert _modules_digest(ansible_core) == before
    return copy, drafts


def test_drafts_are_pasted(pasted: tuple[Path, dict[str, str]]) -> None:
    copy, drafts = pasted
    assert set(drafts) == MISSING
    for name, draft in drafts.items():
        source = _module(copy, name).read_text()
        assert draft in source, name
        assert "RETURN = r" in draft, name
        assert MARKER in draft, name


def test_ac8_validate_modules_passes(pasted: tuple[Path, dict[str, str]]) -> None:
    copy, _ = pasted
    proc = _validate(copy, MISSING)
    errors = [line for line in proc.stdout.splitlines() if line.startswith("ERROR")]
    assert proc.returncode == 0, "\n".join(errors) or proc.stdout + proc.stderr
    assert errors == []


def test_validate_modules_would_catch_a_bad_draft(
    ansible_core: Path, tmp_path: Path
) -> None:
    # Positive control: the same run rejects a draft with an invalid type.
    copy = _copy(ansible_core, tmp_path)
    result = CliRunner().invoke(main, ["draft", str(ansible_core), "fetch"])
    bad = result.stdout.replace("type: ", "type: bogus_", 1)
    path = _module(copy, "fetch")
    path.write_text(paste_draft(path.read_text(), bad))
    proc = _validate(copy, {"fetch"})
    assert proc.returncode != 0
    assert "return-syntax-error" in proc.stdout + proc.stderr
