"""Tests for the acceptance-test harness itself (plan task 29).

The harness fetches a pinned ansible-core commit into a cache directory. These
tests use a local git repository as the "remote", so they need no network.
"""

import ast
import subprocess
from pathlib import Path

import pytest

from tests.acceptance import checkout
from tests.acceptance.checkout import (
    COMMIT,
    REPOSITORY,
    CheckoutError,
    Pin,
    cache_root,
    ensure_checkout,
    pinned,
    resolve_checkout,
)

ACCEPTANCE = Path(__file__).resolve().parent / "acceptance"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def remote(tmp_path: Path) -> tuple[Path, str]:
    """A tiny ansible-core lookalike with one commit; returns (path, sha)."""
    repo = tmp_path / "remote"
    (repo / "lib" / "ansible" / "modules").mkdir(parents=True)
    (repo / "lib" / "ansible" / "modules" / "ping.py").write_text("# ping\n")
    _git(tmp_path, "init", "-q", str(repo))
    _git(repo, "add", ".")
    _git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-q",
        "-m",
        "one",
    )
    return repo, _git(repo, "rev-parse", "HEAD")


# --- the pin ------------------------------------------------------------------------


def test_the_pin_is_a_full_commit_of_ansible_core() -> None:
    assert REPOSITORY == "https://github.com/ansible/ansible.git"
    assert len(COMMIT) == 40
    assert int(COMMIT, 16) >= 0


def test_pin_defaults_and_overrides() -> None:
    assert pinned({}) == Pin(REPOSITORY, COMMIT)
    other = "a" * 40
    assert pinned(
        {"DR_ANSIBLE_CORE_REPOSITORY": "file:///x", "DR_ANSIBLE_CORE_COMMIT": other}
    ) == Pin("file:///x", other)


@pytest.mark.parametrize("bad", ["devel", "abc123", "g" * 40, "A" * 39])
def test_the_pin_must_be_a_full_sha(bad: str) -> None:
    with pytest.raises(CheckoutError, match="full 40-character commit"):
        pinned({"DR_ANSIBLE_CORE_COMMIT": bad})


def test_cache_root(tmp_path: Path) -> None:
    assert cache_root({"DR_ANSIBLE_CACHE_DIR": str(tmp_path)}) == tmp_path
    assert cache_root({"XDG_CACHE_HOME": str(tmp_path)}) == tmp_path / "dr-ansible"
    assert cache_root({}) == Path.home() / ".cache" / "dr-ansible"


# --- fetching -----------------------------------------------------------------------


def test_fetches_the_pinned_commit(tmp_path: Path, remote: tuple[Path, str]) -> None:
    repo, sha = remote
    path = ensure_checkout(Pin(repo.as_uri(), sha), tmp_path / "cache")
    assert path == tmp_path / "cache" / "ansible-core" / sha
    assert (path / "lib" / "ansible" / "modules" / "ping.py").is_file()
    assert _git(path, "rev-parse", "HEAD") == sha
    assert _git(path, "rev-list", "--count", "HEAD") == "1"  # shallow


def test_a_cached_checkout_is_reused(
    tmp_path: Path, remote: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, sha = remote
    pin = Pin(repo.as_uri(), sha)
    first = ensure_checkout(pin, tmp_path / "cache")

    def no_fetch(*args: object, **kwargs: object) -> None:
        raise AssertionError("fetched again")

    monkeypatch.setattr(checkout, "_fetch", no_fetch)
    assert ensure_checkout(pin, tmp_path / "cache") == first


def test_a_damaged_cache_is_fetched_again(
    tmp_path: Path, remote: tuple[Path, str]
) -> None:
    repo, sha = remote
    pin = Pin(repo.as_uri(), sha)
    path = ensure_checkout(pin, tmp_path / "cache")
    (path / "lib" / "ansible" / "modules" / "ping.py").unlink()
    (path / checkout.MARKER).unlink()
    assert ensure_checkout(pin, tmp_path / "cache") == path
    assert (path / "lib" / "ansible" / "modules" / "ping.py").is_file()


def test_an_unknown_commit_fails_cleanly(
    tmp_path: Path, remote: tuple[Path, str]
) -> None:
    repo, _ = remote
    with pytest.raises(CheckoutError, match="cannot fetch"):
        ensure_checkout(Pin(repo.as_uri(), "0" * 40), tmp_path / "cache")
    leftovers = list((tmp_path / "cache" / "ansible-core").iterdir())
    assert leftovers == []  # no half-finished checkout is left behind


def test_a_tree_that_is_not_ansible_core_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "remote"
    repo.mkdir()
    (repo / "README").write_text("x\n")
    _git(tmp_path, "init", "-q", str(repo))
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "x")
    sha = _git(repo, "rev-parse", "HEAD")
    with pytest.raises(CheckoutError, match="not an ansible-core checkout"):
        ensure_checkout(Pin(repo.as_uri(), sha), tmp_path / "cache")


# --- an existing checkout -----------------------------------------------------------


def test_an_existing_checkout_can_be_named(
    tmp_path: Path, remote: tuple[Path, str]
) -> None:
    repo, _ = remote
    env = {"DR_ANSIBLE_CORE_CHECKOUT": str(repo)}
    assert resolve_checkout(env) == repo


def test_a_named_checkout_must_be_ansible_core(tmp_path: Path) -> None:
    with pytest.raises(CheckoutError, match="not an ansible-core checkout"):
        resolve_checkout({"DR_ANSIBLE_CORE_CHECKOUT": str(tmp_path)})


def test_resolve_fetches_the_pin_by_default(
    tmp_path: Path, remote: tuple[Path, str]
) -> None:
    repo, sha = remote
    env = {
        "DR_ANSIBLE_CACHE_DIR": str(tmp_path / "cache"),
        "DR_ANSIBLE_CORE_REPOSITORY": repo.as_uri(),
        "DR_ANSIBLE_CORE_COMMIT": sha,
    }
    assert resolve_checkout(env) == tmp_path / "cache" / "ansible-core" / sha


# --- acceptance tests stay opt-in ---------------------------------------------------


def test_every_acceptance_module_is_marked() -> None:
    modules = sorted(ACCEPTANCE.glob("test_*.py"))
    assert modules
    for module in modules:
        tree = ast.parse(module.read_text())
        marks = [
            ast.unparse(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(getattr(t, "id", None) == "pytestmark" for t in node.targets)
        ]
        assert len(marks) == 1, module.name
        assert marks[0] in {
            "pytest.mark.acceptance",
            "[pytest.mark.acceptance, pytest.mark.runtime]",
        }, module.name


def test_acceptance_tests_are_skipped_by_default() -> None:
    proc = subprocess.run(
        ["pytest", "--collect-only", "-q", str(ACCEPTANCE)],
        capture_output=True,
        text=True,
        check=False,
        cwd=ACCEPTANCE.parent.parent,
    )
    assert "deselected" in proc.stdout, proc.stdout
    assert "test_checkout.py::" not in proc.stdout.split("deselected")[0]
