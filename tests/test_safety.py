"""Safety: without ``--run`` nothing is imported, executed, spawned or written (AC-10).

Each check runs dr-ansible in a fresh Python process with an audit hook
(:pep:`578`) installed before dr_ansible is imported. The hook sees every
import, every code object executed, every process started and every file
opened for writing, so "nothing ran" is observed rather than inferred. The
fixtures include ``raises_on_import.py``, which raises if it is ever imported
or executed.

The positive controls prove the harness would catch each kind of violation.
"""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CORE = FIXTURES / "core"
TRAP = CORE / "lib" / "ansible" / "modules" / "raises_on_import.py"
WRITTEN = TRAP.parent / "zz_written.txt"

#: Runs ``ACTION`` under an audit hook and prints the violations as JSON.
HARNESS = textwrap.dedent(
    """
    import json, os, sys

    FIXTURES = os.path.realpath(sys.argv[1])
    ACTION = sys.argv[2]
    violations = []
    # ansible-core runs `ldconfig -p` (ctypes.util.find_library) when it is
    # imported: a library lookup, not analysed code. Reported, not failed.
    ignored = []
    SPAWN_EVENTS = {
        "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn",
        "os.spawn", "os.fork", "os.forkpty", "pty.spawn", "os.startfile",
    }

    def inside(path):
        try:
            return os.path.realpath(os.fsdecode(path)).startswith(FIXTURES + os.sep)
        except (TypeError, ValueError):
            return False

    def hook(event, args):
        if event == "import" and args[1] and inside(args[1]):
            violations.append(f"import {args[0]} from {args[1]}")
        elif event == "exec" and inside(getattr(args[0], "co_filename", "")):
            violations.append(f"exec {args[0].co_filename}")
        elif event in SPAWN_EVENTS:
            frame = sys._getframe(1)
            while frame is not None:
                if frame.f_code.co_filename.endswith(os.path.join("ctypes", "util.py")):
                    ignored.append(f"{event} {args[:2]!r} from ctypes.util")
                    return
                frame = frame.f_back
            violations.append(f"{event} {args[:2]!r}")
        elif event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            mode = args[1] or "r"
            flags = args[2] or 0
            writing = any(c in str(mode) for c in "wax+") or flags & (
                os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
            )
            if writing and inside(args[0]):
                violations.append(f"write {args[0]}")

    sys.dont_write_bytecode = False  # an import would also leave __pycache__ behind
    sys.addaudithook(hook)
    try:
        exec(ACTION)
    except BaseException as exc:  # report, then let the test decide
        violations.append(f"raised {type(exc).__name__}: {exc}")
    print(json.dumps({"violations": violations, "ignored": ignored}))
    """
)

#: Every command over every fixture module, as one action (one process).
COMMANDS = textwrap.dedent(
    """
    from dr_ansible.cli import main
    from dr_ansible.discovery import detect_project, discover_modules

    def cli(*args):
        try:
            main([*args], standalone_mode=False)
        except SystemExit:
            pass

    for root in sorted(p for p in FIXTURES_DIR.iterdir() if p.is_dir()):
        for fmt in ("table", "json", "markdown"):
            cli("audit", str(root), "--format", fmt)
        for module in discover_modules(detect_project(root)):
            if module.language != "python":
                continue
            cli("returns", str(root), module.fqcn, "--format", "json")
            cli("returns", str(root), module.fqcn, "--include-common")
            cli("draft", str(root), module.fqcn)
            cli("draft", str(root), module.fqcn, "--full")
    """
)


def _run(action: str) -> list[str]:
    prelude = f"from pathlib import Path\nFIXTURES_DIR = Path({str(FIXTURES)!r})\n"
    proc = subprocess.run(
        [sys.executable, "-c", HARNESS, str(FIXTURES), prelude + action],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    # The only exemption: ansible-core's own shared-library lookup.
    for item in result["ignored"]:
        assert "ldconfig" in item or "gcc" in item or "'ld'" in item, item
    violations = result["violations"]
    assert isinstance(violations, list)
    return violations


def _pycache() -> list[Path]:
    return sorted(FIXTURES.rglob("__pycache__"))


# --- the safety checks --------------------------------------------------------------


def test_no_command_imports_executes_spawns_or_writes() -> None:
    before = _pycache()
    assert _run(COMMANDS) == []
    assert _pycache() == before


def test_the_trap_is_audited_from_its_source() -> None:
    action = textwrap.dedent(
        """
        import json
        from click.testing import CliRunner
        from dr_ansible.cli import main
        args = ["returns", str(FIXTURES_DIR / "core"), "raises_on_import"]
        result = CliRunner().invoke(main, [*args, "--format", "json"])
        (module,) = json.loads(result.stdout)["modules"]
        assert module["return_status"] == "missing", module
        assert [k["name"] for k in module["keys"]] == ["trapped"], module
        """
    )
    assert _run(action) == []


# --- positive controls: the harness catches each kind of violation ------------------


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (
            f"import runpy; runpy.run_path({str(TRAP)!r})",
            "exec ",
        ),
        (
            "import importlib.util as u\n"
            f"s = u.spec_from_file_location('trap', {str(TRAP)!r})\n"
            "s.loader.exec_module(u.module_from_spec(s))",
            "exec ",
        ),
        (
            # A plain import has no filename in its audit event; executing the
            # module and writing its bytecode give it away.
            f"import sys; sys.path.insert(0, {str(TRAP.parent)!r}); import helper",
            "exec ",
        ),
        ("import subprocess; subprocess.run(['true'])", "subprocess.Popen"),
        (
            # The ctypes exemption covers only spawns made inside ctypes.util.
            "import ctypes.util, os; ctypes.util.find_library('c'); os.system('true')",
            "os.system",
        ),
        ("import os; os.system('true')", "os.system"),
        (f"open({str(WRITTEN)!r}, 'w')", "write "),
    ],
)
def test_the_harness_catches_violations(action: str, expected: str) -> None:
    try:
        violations = _run(action)
    finally:
        WRITTEN.unlink(missing_ok=True)
        for cache in _pycache():
            if cache.parent == TRAP.parent:
                for file in cache.iterdir():
                    file.unlink()
                cache.rmdir()
    assert any(v.startswith(expected) for v in violations), violations
