"""Find the modules in an ansible-core checkout or a collection (FR-1, FR-2).

Discovery only looks at file names and metadata files (``galaxy.yml``,
``MANIFEST.json``). It never reads, imports or runs module code.
"""

import json
import keyword
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from dr_ansible.model import Language, ModuleInfo

CORE_NAMESPACE = "ansible"
CORE_NAME = "builtin"
SIDECAR_SUFFIXES = (".yml", ".yaml")


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

    @property
    def collection(self) -> str:
        return f"{self.namespace}.{self.name}"


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
    )


def discover_modules(project: Project) -> list[ModuleInfo]:
    """List the project's modules, sorted by name, with their related files (FR-2).

    Python modules are ``*.py`` files other than ``__init__.py``. A ``*.ps1``
    file is a PowerShell module; a ``.py`` file beside it is its docs stub,
    not a second module. Subdirectories of the modules directory are not
    scanned.
    """
    if not project.modules_dir.is_dir():
        return []

    files = {p.name: p for p in project.modules_dir.iterdir() if p.is_file()}
    stems: dict[str, Language] = {}
    for filename, file in files.items():
        if file.suffix == ".ps1":
            stems[file.stem] = Language.POWERSHELL
        elif file.suffix == ".py" and filename != "__init__.py":
            stems.setdefault(file.stem, Language.PYTHON)

    return [_module_info(project, stem, stems[stem]) for stem in sorted(stems)]


def _module_info(project: Project, stem: str, language: Language) -> ModuleInfo:
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
    return ModuleInfo(
        name=stem,
        fqcn=f"{project.collection}.{stem}",
        language=language,
        module_path=project.modules_dir / f"{stem}{suffix}",
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
