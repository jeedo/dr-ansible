"""NFR-7 performance and NFR-6 determinism on ansible-core (plan task 34)."""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.acceptance

#: NFR-7: a static audit of all of ansible-core finishes in under 10 seconds.
AUDIT_LIMIT_SECONDS = 10.0
#: The installed console script, as a user runs it.
DR_ANSIBLE = (
    shutil.which("dr-ansible", path=str(Path(sys.executable).parent)) or "dr-ansible"
)


def _cli(*args: str | Path, seed: str = "0") -> subprocess.CompletedProcess[str]:
    """Run dr-ansible in a fresh process, as a user would."""
    env = {**os.environ, "PYTHONHASHSEED": seed}
    return subprocess.run(
        [DR_ANSIBLE, *map(str, args)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=300,
    )


def test_nfr7_full_audit_is_fast(ansible_core: Path) -> None:
    # The best of two runs, interpreter start-up included, so a busy machine
    # does not fail the check by itself.
    timings = []
    for _ in range(2):
        start = time.perf_counter()
        proc = _cli("audit", ansible_core, "--format", "json")
        timings.append(time.perf_counter() - start)
        assert proc.returncode == 1, proc.stderr  # ansible-core has findings
    assert min(timings) < AUDIT_LIMIT_SECONDS, timings


@pytest.mark.parametrize(
    "args",
    [
        ("audit", "--format", "json"),
        ("audit", "--format", "table"),
        ("audit", "--format", "markdown"),
        ("returns", "fetch", "--format", "json"),
        ("returns", "stat", "--include-common", "--format", "json"),
        ("returns", "copy"),
        ("draft", "fetch"),
        ("draft", "stat", "--full"),
        ("draft", "lineinfile"),
    ],
    ids=lambda args: "-".join(args),
)
def test_nfr6_output_is_identical_across_processes(
    ansible_core: Path, args: tuple[str, ...]
) -> None:
    # Different hash seeds change the iteration order of sets and dicts built
    # from them, so any ordering that is not sorted shows up as a difference.
    command, *rest = args
    first = _cli(command, ansible_core, *rest, seed="1")
    second = _cli(command, ansible_core, *rest, seed="2")
    assert first.stdout
    assert first.returncode == second.returncode
    assert first.stdout == second.stdout
