"""Core data model shared by every stage of the pipeline.

All types are frozen dataclasses. Collections are stored as tuples or
frozensets and sorted on construction, so reports are deterministic whatever
order the analysers produced them in (NFR-6). Each type checks its own
invariants in ``__post_init__`` so that an inconsistent report fails where it
is built rather than where it is printed.
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

type JSONValue = (
    bool | int | float | str | list[JSONValue] | dict[str, JSONValue] | None
)


class Language(StrEnum):
    PYTHON = "python"
    POWERSHELL = "powershell"


class ReturnStatus(StrEnum):
    """A module's ``RETURN`` status (FR-5, FR-6, NFR-8)."""

    MISSING = "missing"
    PLACEHOLDER = "placeholder"
    INVALID = "invalid"
    PRESENT = "present"
    EXEMPT = "exempt"
    ERROR = "error"
    UNSUPPORTED = "unsupported"


#: Statuses the documentation audit can produce. ``error`` and ``unsupported``
#: describe the whole module report, not its documentation.
DOC_STATUSES = frozenset(
    {
        ReturnStatus.MISSING,
        ReturnStatus.PLACEHOLDER,
        ReturnStatus.INVALID,
        ReturnStatus.PRESENT,
        ReturnStatus.EXEMPT,
    }
)


class KeyStatus(StrEnum):
    """How one return key's documentation compares with the evidence (FR-17)."""

    OK = "ok"
    UNDOCUMENTED = "undocumented"
    STALE = "stale"
    TEST_ONLY = "test-only"


class ReturnType(StrEnum):
    """``type`` values allowed by validate-modules' ``return_schema``."""

    BOOL = "bool"
    COMPLEX = "complex"
    DICT = "dict"
    FLOAT = "float"
    INT = "int"
    LIST = "list"
    RAW = "raw"
    STR = "str"


class Outcome(StrEnum):
    """Whether a key is returned on a success or a failure path (FR-11)."""

    SUCCESS = "success"
    FAILURE = "failure"


class ResultState(StrEnum):
    """The state of a task result a key was observed in (FR-15)."""

    OK = "ok"
    CHANGED = "changed"
    FAILED = "failed"


def _set(obj: object, name: str, value: object) -> None:
    """Assign to a frozen dataclass field during ``__post_init__``."""
    object.__setattr__(obj, name, value)


def _check_line(line: int) -> None:
    if line < 1:
        raise ValueError(f"line must be 1 or greater, got {line}")


def _check_key_path(path: str) -> None:
    if not path or any(not part for part in path.split(".")):
        raise ValueError(f"key path must be a dotted name, got {path!r}")


def _check_unique(paths: Iterable[str], what: str) -> None:
    seen: set[str] = set()
    for path in paths:
        if path in seen:
            raise ValueError(f"duplicate {what} {path!r}")
        seen.add(path)


@dataclass(frozen=True, slots=True, order=True)
class Location:
    """A position in a source or test file."""

    file: Path
    line: int

    def __post_init__(self) -> None:
        _check_line(self.line)


@dataclass(frozen=True, slots=True)
class Sample:
    """A sample value. Wrapped so that a ``null`` sample differs from no sample.

    Samples compare and hash by their canonical JSON form, so list and dict
    samples can live in sets, and ``Sample(1)`` differs from ``Sample(True)``.
    """

    value: JSONValue

    def _canonical(self) -> str:
        return json.dumps(self.value, sort_keys=True)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Sample):
            return NotImplemented
        return self._canonical() == other._canonical()

    def __hash__(self) -> int:
        return hash(self._canonical())


@dataclass(frozen=True, slots=True, kw_only=True)
class ModuleInfo:
    """A discovered module and the files that belong to it (FR-1 to FR-3)."""

    name: str
    fqcn: str
    language: Language
    module_path: Path
    aliases: tuple[str, ...] = ()
    sidecar_path: Path | None = None
    action_path: Path | None = None
    test_target: Path | None = None

    def __post_init__(self) -> None:
        for name in ("name", "fqcn"):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")
        _set(self, "aliases", tuple(sorted(set(self.aliases))))

    @property
    def names(self) -> tuple[str, ...]:
        """Every name the module answers to: short name, FQCN and aliases."""
        return tuple(sorted({self.name, self.fqcn, *self.aliases}))

    def paths(self) -> dict[str, Path]:
        """The module's known files, by role, omitting the ones it lacks."""
        candidates = {
            "module": self.module_path,
            "sidecar": self.sidecar_path,
            "action": self.action_path,
            "test_target": self.test_target,
        }
        return {role: path for role, path in candidates.items() if path is not None}


@dataclass(frozen=True, slots=True, kw_only=True)
class DocumentedKey:
    """One key from a module's ``RETURN`` block (FR-7). ``path`` is dotted."""

    path: str
    type: str | None = None
    returned: str | None = None
    has_description: bool = False
    elements: str | None = None

    def __post_init__(self) -> None:
        _check_key_path(self.path)

    @property
    def parts(self) -> tuple[str, ...]:
        return tuple(self.path.split("."))


@dataclass(frozen=True, slots=True, kw_only=True)
class DocResult:
    """The documentation audit result for one module (FR-5 to FR-7)."""

    status: ReturnStatus
    keys: tuple[DocumentedKey, ...] = ()
    raw_text: str | None = None
    problems: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in DOC_STATUSES:
            raise ValueError(f"status {self.status} is not a documentation status")
        if self.keys and self.status not in {ReturnStatus.PRESENT, ReturnStatus.EXEMPT}:
            raise ValueError(f"a {self.status} RETURN cannot have documented keys")
        keys = tuple(sorted(self.keys, key=lambda k: k.path))
        _check_unique((k.path for k in keys), "documented key")
        _set(self, "keys", keys)
        _set(self, "problems", tuple(self.problems))

    def key(self, path: str) -> DocumentedKey | None:
        return next((k for k in self.keys if k.path == path), None)


@dataclass(frozen=True, slots=True, kw_only=True)
class StaticKey:
    """A return key found by static analysis of module or action code (FR-11)."""

    path: str
    file: Path
    line: int
    outcome: Outcome
    inferred_type: ReturnType | None = None
    condition: str | None = None
    #: The value, when the code sets it to a plain literal (a draft's fallback sample).
    literal: Sample | None = None

    def __post_init__(self) -> None:
        _check_key_path(self.path)
        _check_line(self.line)


@dataclass(frozen=True, slots=True, kw_only=True)
class Unresolved:
    """A key static analysis could not name, such as a computed key (FR-12)."""

    file: Path
    line: int
    reason: str

    def __post_init__(self) -> None:
        _check_line(self.line)
        if not self.reason:
            raise ValueError("reason must not be empty")


@dataclass(frozen=True, slots=True, kw_only=True)
class Observation:
    """Combined test evidence for one return key (FR-13 to FR-15)."""

    path: str
    count: int
    types: frozenset[str] = frozenset()
    results: frozenset[ResultState] = frozenset()
    sample: Sample | None = None
    sources: tuple[Location, ...] = field(default=())

    def __post_init__(self) -> None:
        _check_key_path(self.path)
        if self.count < 1:
            raise ValueError(f"count must be 1 or greater, got {self.count}")
        _set(self, "types", frozenset(self.types))
        _set(self, "results", frozenset(self.results))
        _set(self, "sources", tuple(sorted(self.sources)))


@dataclass(frozen=True, slots=True, kw_only=True)
class KeyReport:
    """The reconciled view of one return key (FR-17)."""

    name: str
    status: KeyStatus
    documented: DocumentedKey | None = None
    static: tuple[StaticKey, ...] = ()
    observed: Observation | None = None

    def __post_init__(self) -> None:
        _check_key_path(self.name)
        evidence_paths = [s.path for s in self.static]
        if self.documented is not None:
            evidence_paths.append(self.documented.path)
        if self.observed is not None:
            evidence_paths.append(self.observed.path)
        if any(path != self.name for path in evidence_paths):
            raise ValueError(f"evidence path does not match key {self.name!r}")
        _set(self, "static", tuple(sorted(self.static, key=lambda s: (s.file, s.line))))
        self._check_status()

    def _check_status(self) -> None:
        documented = self.documented is not None
        static = bool(self.static)
        observed = self.observed is not None
        valid = {
            KeyStatus.OK: documented and (static or observed),
            KeyStatus.UNDOCUMENTED: not documented and (static or observed),
            KeyStatus.STALE: documented and not static and not observed,
            KeyStatus.TEST_ONLY: observed and not static,
        }[self.status]
        if not valid:
            raise ValueError(
                f"status {self.status} does not match evidence for {self.name!r}"
                f" (documented={documented}, static={static}, observed={observed})"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class ModuleReport:
    """Everything dr-ansible knows about one module; the unit of output."""

    module: ModuleInfo
    return_status: ReturnStatus
    keys: tuple[KeyReport, ...] = ()
    unresolved: tuple[Unresolved, ...] = ()
    #: Modules (or delegated action plugins) whose returns this module passes on.
    inherits_from: tuple[str, ...] = ()
    error: str | None = None

    def __post_init__(self) -> None:
        if (self.return_status is ReturnStatus.ERROR) != bool(self.error):
            raise ValueError("an error status needs an error reason, and only it")
        powershell = self.module.language is Language.POWERSHELL
        if (self.return_status is ReturnStatus.UNSUPPORTED) != powershell:
            raise ValueError("unsupported is the status for, and only for, PowerShell")
        keys = tuple(sorted(self.keys, key=lambda k: k.name))
        _check_unique((k.name for k in keys), "key")
        _set(self, "keys", keys)
        _set(self, "inherits_from", tuple(sorted(set(self.inherits_from))))
        _set(
            self,
            "unresolved",
            tuple(sorted(self.unresolved, key=lambda u: (u.file, u.line, u.reason))),
        )

    @property
    def fqcn(self) -> str:
        return self.module.fqcn
