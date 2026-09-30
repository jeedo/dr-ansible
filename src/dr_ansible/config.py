"""Configuration: built-in defaults, optionally overridden by a TOML file (NFR-9).

dr-ansible looks in the root of the tree it audits for ``dr-ansible.toml``,
then for a ``[tool.dr-ansible]`` table in ``pyproject.toml``. The first one
found is used on its own; the two are never merged. Each list setting can be
replaced (``exempt-modules = [...]``) or added to
(``extend-exempt-modules = [...]``), in the style of ruff's ``extend-select``.
"""

import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_FILENAME = "dr-ansible.toml"
PYPROJECT_FILENAME = "pyproject.toml"
TOOL_TABLE = "dr-ansible"

#: Modules expected to return nothing, reported as ``exempt`` (FR-6, AC-3).
DEFAULT_EXEMPT_MODULES = frozenset(
    {
        "gather_facts",
        "import_playbook",
        "import_role",
        "import_tasks",
        "include_role",
        "include_tasks",
    }
)

#: Return values common to all modules, excluded by default (FR-18).
DEFAULT_COMMON_RETURN_KEYS = frozenset(
    {
        "changed",
        "deprecations",
        "diff",
        "exception",
        "failed",
        "invocation",
        "msg",
        "skipped",
        "warnings",
    }
)

#: Case-insensitive regular expressions for keys whose values are redacted (FR-16).
DEFAULT_REDACT_PATTERNS = ("key", "password", "secret", "token")

#: The ansible-test container used by ``--run`` (FR-14).
DEFAULT_DOCKER_IMAGE = "default"

_LIST_SETTINGS = ("exempt-modules", "common-return-keys", "redact-patterns")
_KNOWN_KEYS = frozenset(
    {*_LIST_SETTINGS, *(f"extend-{name}" for name in _LIST_SETTINGS), "docker-image"}
)


class ConfigError(ValueError):
    """The configuration file is missing, unreadable or invalid."""


def _compile(patterns: Iterable[str]) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p, re.IGNORECASE) for p in patterns)


@dataclass(frozen=True, slots=True, kw_only=True)
class Config:
    """Settings for one run. ``source`` is the file they came from, if any."""

    exempt_modules: frozenset[str] = DEFAULT_EXEMPT_MODULES
    common_return_keys: frozenset[str] = DEFAULT_COMMON_RETURN_KEYS
    redact_patterns: tuple[str, ...] = DEFAULT_REDACT_PATTERNS
    docker_image: str = DEFAULT_DOCKER_IMAGE
    source: Path | None = field(default=None, compare=False)
    _redact_regexes: tuple[re.Pattern[str], ...] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "_redact_regexes", _compile(self.redact_patterns))

    def is_sensitive(self, key: str) -> bool:
        """Whether values under ``key`` must be redacted (FR-16)."""
        return any(regex.search(key) for regex in self._redact_regexes)


def load_config(root: Path, config_file: Path | None = None) -> Config:
    """Load settings for the tree at ``root``, or from ``config_file`` if given.

    Raises :class:`ConfigError` if the file is invalid, or if ``config_file``
    is given and does not exist.
    """
    if config_file is not None:
        if not config_file.is_file():
            raise ConfigError(f"{config_file}: config file not found")
        return _from_file(config_file)

    for candidate in (root / CONFIG_FILENAME, root / PYPROJECT_FILENAME):
        if candidate.is_file():
            config = _from_file(candidate)
            if config.source is not None:
                return config
    return Config()


def _from_file(path: Path) -> Config:
    data = _read_toml(path)
    if path.name != PYPROJECT_FILENAME:
        return _build(data, path)
    tool = data.get("tool", {})
    if TOOL_TABLE not in tool:
        return Config()
    table = tool[TOOL_TABLE]
    if not isinstance(table, dict):
        raise ConfigError(f"{path}: [tool.{TOOL_TABLE}] must be a table")
    return _build(table, path)


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read config file: {exc}") from exc


def _build(table: dict[str, Any], path: Path) -> Config:
    unknown = sorted(set(table) - _KNOWN_KEYS)
    if unknown:
        known = ", ".join(sorted(_KNOWN_KEYS))
        raise ConfigError(f"{path}: unknown setting {unknown[0]!r} (known: {known})")

    defaults = Config()
    exempt = _merged(table, "exempt-modules", defaults.exempt_modules, path)
    common = _merged(table, "common-return-keys", defaults.common_return_keys, path)
    redact = _merged(table, "redact-patterns", defaults.redact_patterns, path)
    for pattern in redact:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigError(
                f"{path}: redact-patterns: invalid regular expression"
                f" {pattern!r}: {exc}"
            ) from exc

    docker_image = table.get("docker-image", defaults.docker_image)
    if not isinstance(docker_image, str) or not docker_image:
        raise ConfigError(f"{path}: docker-image must be a non-empty string")

    return Config(
        exempt_modules=frozenset(exempt),
        common_return_keys=frozenset(common),
        redact_patterns=tuple(redact),
        docker_image=docker_image,
        source=path,
    )


def _merged(
    table: dict[str, Any], name: str, default: Iterable[str], path: Path
) -> list[str]:
    """``name`` (or the default) followed by ``extend-name``, without duplicates."""
    base = _string_list(table, name, path)
    extra = _string_list(table, f"extend-{name}", path) or []
    values = list(default) if base is None else base
    return list(dict.fromkeys([*values, *extra]))


def _string_list(table: dict[str, Any], name: str, path: Path) -> list[str] | None:
    if name not in table:
        return None
    value = table[name]
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ConfigError(f"{path}: {name} must be a list of non-empty strings")
    return value
