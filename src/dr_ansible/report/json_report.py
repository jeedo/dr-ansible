"""The machine-readable report (FR-21).

The shape is described by ``schema/report.schema.json``; bump
:data:`SCHEMA_VERSION` on any incompatible change. Samples are redacted again
here, whatever produced them (FR-16), and code literals are not included.
"""

import json
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path

from dr_ansible.config import Config
from dr_ansible.mining.redact import redact_sample
from dr_ansible.model import JSONValue, KeyReport, ModuleReport, Observation
from dr_ansible.report.summary import by_fqcn, counts, relative

SCHEMA_VERSION = 1


def reports_to_json(reports: Iterable[ModuleReport], root: Path, config: Config) -> str:
    """The reports as a JSON document; paths are relative to ``root`` when inside it."""
    document = {
        "schema_version": SCHEMA_VERSION,
        "modules": [_module(r, root, config) for r in by_fqcn(reports)],
    }
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def _module(report: ModuleReport, root: Path, config: Config) -> dict[str, JSONValue]:
    paths = report.module.paths()
    return {
        "module": report.fqcn,
        "name": report.module.name,
        "paths": {role: relative(path, root) for role, path in paths.items()},
        "return_status": str(report.return_status),
        "error": report.error,
        "inherits_from": list(report.inherits_from),
        "counts": dict(asdict(counts(report))),
        "unresolved": [
            {"file": relative(u.file, root), "line": u.line, "reason": u.reason}
            for u in report.unresolved
        ],
        "keys": [_key(k, root, config) for k in report.keys],
    }


def _key(key: KeyReport, root: Path, config: Config) -> dict[str, JSONValue]:
    documented = key.documented
    return {
        "name": key.name,
        "status": str(key.status),
        "documented": None
        if documented is None
        else {
            "path": documented.path,
            "type": documented.type,
            "returned": documented.returned,
            "has_description": documented.has_description,
            "elements": documented.elements,
        },
        "static": [
            {
                "file": relative(s.file, root),
                "line": s.line,
                "path": s.path,
                "inferred_type": None
                if s.inferred_type is None
                else str(s.inferred_type),
                "outcome": str(s.outcome),
                "condition": s.condition,
            }
            for s in key.static
        ],
        "observed": None
        if key.observed is None
        else _observed(key.observed, root, config),
    }


def _observed(
    observed: Observation, root: Path, config: Config
) -> dict[str, JSONValue]:
    types: list[JSONValue] = [*sorted(observed.types)]
    results: list[JSONValue] = [*sorted(str(r) for r in observed.results)]
    data: dict[str, JSONValue] = {
        "count": observed.count,
        "types": types,
        "results": results,
    }
    if observed.sample is not None:
        sample = redact_sample(observed.path, observed.sample.value, config)
        if sample is not None:
            data["sample"] = sample.value
    data["sources"] = [
        {"file": relative(s.file, root), "line": s.line} for s in observed.sources
    ]
    return data
