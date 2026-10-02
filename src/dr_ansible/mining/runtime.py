"""Runtime mining: run a module's integration target and record its results (FR-14).

Only ``--run`` gets here, and this is the only place dr-ansible starts a
process (NFR-5). The project is never touched: it is copied to a temporary
directory, and the copy gets the recorder callback and an ``integration.cfg``
that loads it. ``ansible-test integration`` then runs in the copy, in a
container unless ``--local`` is given.

ansible-test replaces the environment of the Ansible processes it starts,
forces its own config file (``<integration>/integration.cfg`` when the
project has one) and overrides the enabled callbacks. So the copy's recorder
enables itself, and both its plugin directory and its output file are named
relative to ``$JUNIT_OUTPUT_DIR``, which ansible-test sets to its results
directory wherever it runs. The recording lands in the results directory,
which ansible-test copies back from the container.
"""

import configparser
import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dr_ansible.config import Config
from dr_ansible.discovery import Layout, Project
from dr_ansible.mining.callback import CALLBACK_DIR, CALLBACK_NAME
from dr_ansible.mining.redact import redact_sample
from dr_ansible.model import Location, ModuleInfo, Observation, ResultState, Sample

#: The recording's file name, in ansible-test's ``data`` results directory.
RECORDING_NAME = "dr-ansible-recording.jsonl"
#: The recorder's directory, under the copy's integration directory.
PLUGIN_SUBDIR = Path("dr_ansible") / "callback_plugins"
#: Where ansible-test's results go, relative to the project root.
RESULTS_DIRS = {
    Layout.CORE: Path("test/results"),
    Layout.COLLECTION: Path("tests/output"),
}
#: Not copied: version control, results of earlier runs and caches.
_SKIPPED_NAMES = frozenset(
    {
        ".git",
        "__pycache__",
        ".tox",
        ".venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)
_RECORD_VERSION = 1

#: Runs a command in a directory and returns its exit code.
type Runner = Callable[[list[str], Path], int]


class RunError(Exception):
    """The integration target cannot be run."""


@dataclass(frozen=True, slots=True)
class RunResult:
    """What one ``ansible-test integration`` run recorded."""

    observations: tuple[Observation, ...]
    records: int
    returncode: int
    problems: tuple[str, ...] = field(default=())


def run_integration(
    project: Project,
    module: ModuleInfo,
    config: Config,
    *,
    docker_image: str | None,
    runner: Runner | None = None,
    ansible_test: str | None = None,
) -> RunResult:
    """Run ``module``'s integration target with the recorder and read what it saw.

    ``docker_image`` is the ansible-test container to use; ``None`` runs
    with ``--local`` on this machine.
    """
    with tempfile.TemporaryDirectory(prefix="dr-ansible-run-") as work:
        copy = prepare_copy(project, module, config, Path(work))
        program = ansible_test or find_ansible_test(copy)
        if program is None:
            raise RunError("ansible-test not found: install ansible-core to use --run")
        assert module.test_target is not None  # checked by prepare_copy
        command = build_command(program, module.test_target.name, docker_image)
        returncode = (runner or default_runner)(command, copy)
        recording = copy / RESULTS_DIRS[project.layout] / "data" / RECORDING_NAME
        records, problems = _read_records(recording)
        observations = _observations(records, project, config)
    return RunResult(observations, len(records), returncode, problems)


def prepare_copy(
    project: Project, module: ModuleInfo, config: Config, work: Path
) -> Path:
    """Copy the project into ``work`` and set up the recorder; return the copy."""
    if module.test_target is None:
        raise RunError(f"{module.fqcn} has no integration target to run")
    if project.layout is Layout.CORE:
        copy = work / "ansible"
    else:  # ansible-test needs the ansible_collections/<ns>/<name> layout
        copy = work / "ansible_collections" / project.namespace / project.name
    results = RESULTS_DIRS[project.layout]
    shutil.copytree(
        project.root, copy, symlinks=True, ignore=_ignore(project.root, results)
    )

    integration = copy / project.targets_dir.parent.relative_to(project.root)
    plugins = integration / PLUGIN_SUBDIR
    plugins.mkdir(parents=True)
    source = (CALLBACK_DIR / f"{CALLBACK_NAME}.py").read_text(encoding="utf-8")
    switch = "\nAUTO_ENABLE = False\n"
    if switch not in source:
        raise RunError(f"cannot enable the {CALLBACK_NAME} callback")
    (plugins / f"{CALLBACK_NAME}.py").write_text(
        source.replace(switch, "\nAUTO_ENABLE = True\n"), encoding="utf-8"
    )
    _write_config(integration / "integration.cfg", module, config)
    return copy


def build_command(
    ansible_test: str, target: str, docker_image: str | None
) -> list[str]:
    command = [ansible_test, "integration", target]
    if docker_image is None:
        return [*command, "--local"]
    return [*command, "--docker", docker_image]


def find_ansible_test(copy: Path) -> str | None:
    """ansible-test from the ansible-core checkout itself, else the installed one."""
    own = copy / "bin" / "ansible-test"
    if own.is_file():
        return str(own)
    beside = Path(sys.executable).parent / "ansible-test"
    if beside.is_file():
        return str(beside)
    return shutil.which("ansible-test")


def default_runner(command: list[str], cwd: Path) -> int:
    """Run ``command``, sending its output to stderr so stdout stays the report."""
    try:
        sys.stderr.fileno()
        output: Any = sys.stderr
    except (AttributeError, OSError, ValueError):
        output = None
    return subprocess.run(
        command, cwd=cwd, stdout=output, stderr=output, check=False
    ).returncode


def load_recording(
    path: Path, project: Project, config: Config
) -> tuple[Observation, ...]:
    """The observations in a recording file, mapped back onto ``project``."""
    records, _ = _read_records(path)
    return _observations(records, project, config)


# --- the copy -----------------------------------------------------------------------


def _ignore(root: Path, results: Path) -> Callable[[str, list[str]], set[str]]:
    skipped_results = root / results

    def ignore(directory: str, names: list[str]) -> set[str]:
        here = Path(directory)
        return {
            name
            for name in names
            if name in _SKIPPED_NAMES or here / name == skipped_results
        }

    return ignore


def _write_config(path: Path, module: ModuleInfo, config: Config) -> None:
    parser = configparser.ConfigParser(interpolation=None)
    if path.is_file():
        parser.read(path, encoding="utf-8")
    plugins = f"$JUNIT_OUTPUT_DIR/../../integration/{PLUGIN_SUBDIR.as_posix()}"
    if not parser.has_section("defaults"):
        parser.add_section("defaults")
    existing = parser.get("defaults", "callback_plugins", fallback="")
    parser.set(
        "defaults", "callback_plugins", f"{plugins}:{existing}" if existing else plugins
    )
    section = f"callback_{CALLBACK_NAME}"
    if not parser.has_section(section):
        parser.add_section(section)
    parser.set(section, "output", f"$JUNIT_OUTPUT_DIR/../data/{RECORDING_NAME}")
    parser.set(section, "module_names", ", ".join(sorted(module.names)))
    parser.set(section, "redact_patterns", ", ".join(config.redact_patterns))
    with path.open("w", encoding="utf-8") as stream:
        parser.write(stream)


# --- the recording ------------------------------------------------------------------


def _read_records(path: Path) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    if not path.is_file():
        return [], ()
    records: list[dict[str, Any]] = []
    problems: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            problems.append(f"{path.name}:{number}: not JSON")
            continue
        if not isinstance(record, dict) or record.get("version") != _RECORD_VERSION:
            problems.append(f"{path.name}:{number}: not a version 1 record")
            continue
        records.append(record)
    return records, tuple(problems)


@dataclass
class _Seen:
    count: int = 0
    types: set[str] = field(default_factory=set)
    results: set[ResultState] = field(default_factory=set)
    sample: Sample | None = None
    has_sample: bool = False
    sources: set[Location] = field(default_factory=set)


def _observations(
    records: Iterable[dict[str, Any]], project: Project, config: Config
) -> tuple[Observation, ...]:
    seen: dict[str, _Seen] = {}
    for record in records:
        try:
            state = ResultState(str(record.get("state")))
        except ValueError:
            continue
        location = _location(record.get("location"), project)
        for key in record.get("keys") or []:
            path = key.get("path") if isinstance(key, dict) else None
            if not isinstance(path, str) or not path:
                continue
            entry = seen.setdefault(path, _Seen())
            entry.count += 1
            if isinstance(key.get("type"), str):
                entry.types.add(key["type"])
            entry.results.add(state)
            if location is not None:
                entry.sources.add(location)
            if "value" in key and not entry.has_sample:
                entry.has_sample = True  # the first value seen (NFR-6)
                entry.sample = redact_sample(path, key["value"], config)  # FR-16
    return tuple(
        Observation(
            path=path,
            count=entry.count,
            types=frozenset(entry.types),
            results=frozenset(entry.results),
            sample=entry.sample,
            sources=tuple(entry.sources),
        )
        for path, entry in sorted(seen.items())
    )


def _location(data: object, project: Project) -> Location | None:
    """Map a task location in the copy (or container) back onto the project."""
    if not isinstance(data, dict):
        return None
    file, line = data.get("file"), data.get("line")
    if not isinstance(file, str) or not isinstance(line, int) or line < 1:
        return None
    marker = f"/{project.targets_dir.relative_to(project.root).as_posix()}/"
    if marker not in file:
        return None
    return Location(project.targets_dir / file.rsplit(marker, 1)[1], line)
