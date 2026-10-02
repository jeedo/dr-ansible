"""Fetch the pinned ansible-core checkout that acceptance tests audit (plan task 29).

The checkout is test *data*: dr-ansible reads it, never imports it. It is a
shallow fetch of one commit, cached under
``<cache>/ansible-core/<commit>`` and reused while it is intact.

Environment variables:

- ``DR_ANSIBLE_CORE_CHECKOUT``: use this existing checkout as it is.
- ``DR_ANSIBLE_CORE_COMMIT`` / ``DR_ANSIBLE_CORE_REPOSITORY``: override the pin.
- ``DR_ANSIBLE_CACHE_DIR``: the cache directory (default
  ``$XDG_CACHE_HOME/dr-ansible``, else ``~/.cache/dr-ansible``).

``python -m tests.acceptance.checkout`` fetches the checkout and prints its path,
for CI to warm its cache.
"""

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

#: Where ansible-core comes from.
REPOSITORY = "https://github.com/ansible/ansible.git"
#: The pinned devel commit (2026-10-02). Bump it deliberately: the acceptance
#: criteria name modules, keys and line numbers from this tree.
COMMIT = "abf5bf59a229e0df2e15a421a5eff83a7b8e3796"
#: Written last, so a checkout without it is incomplete.
MARKER = ".dr-ansible-pin"

_SHA = re.compile(r"[0-9a-f]{40}")


class CheckoutError(Exception):
    """The pinned checkout cannot be fetched or is not ansible-core."""


@dataclass(frozen=True, slots=True)
class Pin:
    repository: str
    commit: str


def pinned(env: Mapping[str, str] = os.environ) -> Pin:
    """The pin, with any overrides from the environment."""
    commit = env.get("DR_ANSIBLE_CORE_COMMIT", COMMIT)
    if not _SHA.fullmatch(commit):
        raise CheckoutError(
            f"DR_ANSIBLE_CORE_COMMIT must be a full 40-character commit, not {commit!r}"
        )
    return Pin(env.get("DR_ANSIBLE_CORE_REPOSITORY", REPOSITORY), commit)


def cache_root(env: Mapping[str, str] = os.environ) -> Path:
    if "DR_ANSIBLE_CACHE_DIR" in env:
        return Path(env["DR_ANSIBLE_CACHE_DIR"])
    if "XDG_CACHE_HOME" in env:
        return Path(env["XDG_CACHE_HOME"]) / "dr-ansible"
    return Path.home() / ".cache" / "dr-ansible"


def resolve_checkout(env: Mapping[str, str] = os.environ) -> Path:
    """The checkout to test: one named in the environment, else the cached pin."""
    if "DR_ANSIBLE_CORE_CHECKOUT" in env:
        path = Path(env["DR_ANSIBLE_CORE_CHECKOUT"])
        _check_tree(path)
        return path
    return ensure_checkout(pinned(env), cache_root(env))


def ensure_checkout(pin: Pin, cache: Path) -> Path:
    """The checkout of ``pin`` under ``cache``, fetched if missing or damaged."""
    target = cache / "ansible-core" / pin.commit
    marker = target / MARKER
    if marker.is_file() and marker.read_text().strip() == pin.commit:
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".fetch-", dir=target.parent))
    try:
        _fetch(pin, work)
        _check_tree(work)
        (work / MARKER).write_text(f"{pin.commit}\n")
        shutil.rmtree(target, ignore_errors=True)
        work.rename(target)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return target


def _fetch(pin: Pin, work: Path) -> None:
    steps = [
        ["init", "-q"],
        ["remote", "add", "origin", pin.repository],
        ["fetch", "-q", "--depth", "1", "origin", pin.commit],
        ["-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD"],
    ]
    for step in steps:
        proc = subprocess.run(
            ["git", "-C", str(work), *step], capture_output=True, text=True, check=False
        )
        if proc.returncode != 0:
            raise CheckoutError(
                f"cannot fetch {pin.commit} from {pin.repository}:"
                f" git {step[0]} failed: {proc.stderr.strip()}"
            )
    head = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if head != pin.commit:
        raise CheckoutError(f"cannot fetch {pin.commit}: got {head}")


def _check_tree(path: Path) -> None:
    if not (path / "lib" / "ansible" / "modules").is_dir():
        raise CheckoutError(f"{path}: not an ansible-core checkout")


if __name__ == "__main__":
    print(resolve_checkout())
