"""Find the modules in an ansible-core checkout or a collection (FR-1 to FR-4).

Discovery only looks at file names, symlinks and metadata files
(``galaxy.yml``, ``MANIFEST.json`` and the routing file). It never reads,
imports or runs module code.
"""

import fnmatch
import json
import keyword
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from dr_ansible.model import Language, ModuleInfo

CORE_NAMESPACE = "ansible"
CORE_NAME = "builtin"
LEGACY_PREFIX = "ansible.legacy."
SIDECAR_SUFFIXES = (".yml", ".yaml")
_GLOB_CHARS = frozenset("*?[")


class DiscoveryError(ValueError):
    """The path is not a tree dr-ansible can audit, or its metadata is invalid."""


class Layout(StrEnum):
    CORE = "ansible-core"
    COLLECTION = "collection"


@dataclass(frozen=True, slots=True, kw_only=True)
class Project:
    """An ansible-core checkout or a collection, and where its parts live."""

    root: Path
    layout: Layout
    namespace: str
    name: str
    modules_dir: Path
    actions_dir: Path
    targets_dir: Path
    routing_file: Path

    @property
    def collection(self) -> str:
        return f"{self.namespace}.{self.name}"

    def qualified_names(self, short: str) -> tuple[str, ...]:
        """The names a module file called ``short`` can be invoked by (FR-3)."""
        if self.layout is Layout.CORE:
            return (short, f"{self.collection}.{short}", f"{LEGACY_PREFIX}{short}")
        return (short, f"{self.collection}.{short}")


def detect_project(path: Path) -> Project:
    """Work out what kind of tree ``path`` is (FR-1).

    ``lib/ansible/modules/`` means an ansible-core checkout. Otherwise
    ``galaxy.yml`` (a source collection) or ``MANIFEST.json`` (a built one)
    means a collection; ``galaxy.yml`` wins if both exist.
    """
    root = path.resolve()
    if not root.exists():
        raise DiscoveryError(f"{path}: does not exist")
    if not root.is_dir():
        raise DiscoveryError(f"{path}: not a directory")

    if (root / "lib" / "ansible" / "modules").is_dir():
        return Project(
            root=root,
            layout=Layout.CORE,
            namespace=CORE_NAMESPACE,
            name=CORE_NAME,
            modules_dir=root / "lib" / "ansible" / "modules",
            actions_dir=root / "lib" / "ansible" / "plugins" / "action",
            targets_dir=root / "test" / "integration" / "targets",
            routing_file=root
            / "lib"
            / "ansible"
            / "config"
            / "ansible_builtin_runtime.yml",
        )

    galaxy = root / "galaxy.yml"
    manifest = root / "MANIFEST.json"
    if galaxy.is_file():
        namespace, name = _identity(_load_yaml(galaxy), galaxy)
    elif manifest.is_file():
        info = _load_json(manifest).get("collection_info")
        if not isinstance(info, dict):
            raise DiscoveryError(f"{manifest}: missing 'collection_info' mapping")
        namespace, name = _identity(info, manifest)
    else:
        raise DiscoveryError(
            f"{path}: not an ansible-core checkout (no lib/ansible/modules/)"
            " or a collection (no galaxy.yml or MANIFEST.json)"
        )

    return Project(
        root=root,
        layout=Layout.COLLECTION,
        namespace=namespace,
        name=name,
        modules_dir=root / "plugins" / "modules",
        actions_dir=root / "plugins" / "action",
        targets_dir=root / "tests" / "integration" / "targets",
        routing_file=root / "meta" / "runtime.yml",
    )


def load_redirects(project: Project) -> dict[str, str]:
    """Module redirects from the routing file: short name -> target FQCN.

    Entries without a ``redirect`` (tombstones, deprecations) are ignored.
    A missing or empty routing file means no redirects.
    """
    if not project.routing_file.is_file():
        return {}
    data = _load_yaml(project.routing_file)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise DiscoveryError(f"{project.routing_file}: expected a mapping")
    routing = data.get("plugin_routing") or {}
    if not isinstance(routing, dict):
        raise DiscoveryError(
            f"{project.routing_file}: plugin_routing must be a mapping"
        )
    modules = routing.get("modules") or {}
    if not isinstance(modules, dict):
        raise DiscoveryError(
            f"{project.routing_file}: plugin_routing.modules must be a mapping"
        )

    redirects: dict[str, str] = {}
    for name, entry in modules.items():
        if not isinstance(entry, dict) or "redirect" not in entry:
            continue
        target = entry["redirect"]
        if not isinstance(target, str) or not target:
            raise DiscoveryError(
                f"{project.routing_file}: redirect for {name!r} must be a string"
            )
        redirects[str(name)] = target
    return redirects


def discover_modules(project: Project) -> list[ModuleInfo]:
    """List the project's modules, sorted by name, with their files and aliases.

    Python modules are ``*.py`` files other than ``__init__.py``. A ``*.ps1``
    file is a PowerShell module; a ``.py`` file beside it is its docs stub,
    not a second module. A ``.py`` symlink to another module in the same
    directory is an alias of that module, as are routing-file redirects that
    lead to it (FR-3). Subdirectories of the modules directory are not scanned.
    """
    if not project.modules_dir.is_dir():
        return []

    stems: dict[str, Language] = {}
    symlink_aliases: dict[str, set[str]] = {}
    real_dir = project.modules_dir.resolve()
    for file in project.modules_dir.iterdir():
        if not file.is_file():
            continue
        if file.suffix == ".ps1":
            stems[file.stem] = Language.POWERSHELL
        elif file.suffix == ".py" and file.name != "__init__.py":
            target = _symlinked_module(file, real_dir)
            if target is not None:
                symlink_aliases.setdefault(target, set()).add(file.stem)
            else:
                stems.setdefault(file.stem, Language.PYTHON)

    redirect_aliases = _redirect_aliases(project, set(stems))
    infos = []
    for stem in sorted(stems):
        alias_shorts = symlink_aliases.get(stem, set()) | redirect_aliases.get(
            stem, set()
        )
        infos.append(_module_info(project, stem, stems[stem], alias_shorts))
    return infos


def filter_modules(
    modules: Sequence[ModuleInfo], patterns: Iterable[str]
) -> list[ModuleInfo]:
    """Modules matching any pattern, by any of their names (FR-4).

    A pattern with ``*``, ``?`` or ``[`` is a glob; anything else must match
    a name exactly. A literal name that matches no module raises
    :class:`DiscoveryError`, since it is almost certainly a typo. No patterns
    selects every module. The input order is kept.
    """
    patterns = list(patterns)
    if not patterns:
        return list(modules)

    selected: set[str] = set()
    for pattern in patterns:
        matches = {m.fqcn for m in modules if _matches(m, pattern)}
        if not matches and not _GLOB_CHARS & set(pattern):
            raise DiscoveryError(f"no module named {pattern!r}")
        selected |= matches
    return [m for m in modules if m.fqcn in selected]


def _matches(module: ModuleInfo, pattern: str) -> bool:
    if _GLOB_CHARS & set(pattern):
        return any(fnmatch.fnmatchcase(name, pattern) for name in module.names)
    return pattern in module.names


def _symlinked_module(file: Path, real_dir: Path) -> str | None:
    """The module a ``.py`` symlink points to, if it is another file here."""
    if not file.is_symlink():
        return None
    target = file.resolve()
    if target.parent != real_dir or target.suffix != ".py" or target == file:
        return None
    return target.stem


def _redirect_aliases(project: Project, stems: set[str]) -> dict[str, set[str]]:
    """Map each module to the redirect names that lead to it, following chains."""
    redirects = load_redirects(project)
    prefix = f"{project.collection}."
    aliases: dict[str, set[str]] = {}
    for source in redirects:
        if source in stems:
            continue  # a real module file wins over a redirect of the same name
        seen = [source]
        current = source
        while current in redirects and current not in stems:
            target = _normalise_target(project, redirects[current])
            if not target.startswith(prefix):
                break  # redirected to another collection
            current = target.removeprefix(prefix)
            if current in seen:
                chain = " -> ".join([*seen, current])
                raise DiscoveryError(f"{project.routing_file}: redirect loop {chain}")
            seen.append(current)
        if current in stems:
            aliases.setdefault(current, set()).add(source)
    return aliases


def _normalise_target(project: Project, target: str) -> str:
    if project.layout is Layout.CORE and target.startswith(LEGACY_PREFIX):
        return f"{project.collection}.{target.removeprefix(LEGACY_PREFIX)}"
    return target


def _module_info(
    project: Project, stem: str, language: Language, alias_shorts: set[str]
) -> ModuleInfo:
    suffix = ".ps1" if language is Language.POWERSHELL else ".py"
    sidecar = None
    if language is Language.PYTHON:
        sidecar = next(
            (
                project.modules_dir / f"{stem}{ext}"
                for ext in SIDECAR_SUFFIXES
                if (project.modules_dir / f"{stem}{ext}").is_file()
            ),
            None,
        )
    action = project.actions_dir / f"{stem}.py"
    target = project.targets_dir / stem
    fqcn = f"{project.collection}.{stem}"
    names = {
        name
        for short in (stem, *alias_shorts)
        for name in project.qualified_names(short)
    }
    return ModuleInfo(
        name=stem,
        fqcn=fqcn,
        language=language,
        module_path=project.modules_dir / f"{stem}{suffix}",
        aliases=tuple(names - {stem, fqcn}),
        sidecar_path=sidecar,
        action_path=action if action.is_file() else None,
        test_target=target if target.is_dir() else None,
    )


def _identity(data: object, source: Path) -> tuple[str, str]:
    if not isinstance(data, dict):
        raise DiscoveryError(f"{source}: expected a mapping")
    parts = []
    for field in ("namespace", "name"):
        value = data.get(field)
        if not isinstance(value, str) or not value:
            raise DiscoveryError(f"{source}: '{field}' must be a non-empty string")
        if not value.isidentifier() or keyword.iskeyword(value):
            raise DiscoveryError(
                f"{source}: '{field}' {value!r} is not a valid collection name part"
            )
        parts.append(value)
    return parts[0], parts[1]


def _load_yaml(path: Path) -> object:
    try:
        return yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise DiscoveryError(f"{path}: cannot read: {exc}") from exc


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise DiscoveryError(f"{path}: cannot read: {exc}") from exc
    if not isinstance(data, dict):
        raise DiscoveryError(f"{path}: expected a JSON object")
    return data
