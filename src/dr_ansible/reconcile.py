"""Reconcile documented, static and observed evidence per key (FR-15, FR-17, FR-18).

Each key gets one status:

- ``ok``: documented, and found in the code or seen in tests;
- ``undocumented``: found in the code, but not in ``RETURN``;
- ``test-only``: seen in tests only, neither documented nor found in the code
  (it is undocumented too, and static analysis missed it);
- ``stale``: documented, but found nowhere.

A documented key that tests confirm but static analysis did not find is
``ok``: the documentation is right, and the report still shows that no code
evidence was found.

Common return values (``changed``, ``msg``, ...) are left out by default, by
their top-level name, whichever source they come from. Inherited modules are
reported, not merged: an action plugin may merge another module's result on
one path only (``fetch`` does so with ``slurp`` on failure), so copying that
module's documentation in would add keys the module does not return.
"""

from collections.abc import Iterable

from dr_ansible.config import Config
from dr_ansible.model import (
    DocResult,
    DocumentedKey,
    KeyReport,
    KeyStatus,
    ModuleInfo,
    ModuleReport,
    Observation,
    Sample,
    StaticKey,
    Unresolved,
)


def merge_observations(observations: Iterable[Observation]) -> tuple[Observation, ...]:
    """One observation per key: counts added, types and results combined (FR-15).

    The sample is the first one in the order given (NFR-6), so callers pass
    observations in file order.
    """
    grouped: dict[str, list[Observation]] = {}
    for observation in observations:
        grouped.setdefault(observation.path, []).append(observation)

    merged = []
    for path, group in sorted(grouped.items()):
        sample: Sample | None = next(
            (o.sample for o in group if o.sample is not None), None
        )
        merged.append(
            Observation(
                path=path,
                count=sum(o.count for o in group),
                types=frozenset().union(*(o.types for o in group)),
                results=frozenset().union(*(o.results for o in group)),
                sample=sample,
                sources=tuple(s for o in group for s in o.sources),
            )
        )
    return tuple(merged)


def reconcile_keys(
    *,
    documented: Iterable[DocumentedKey],
    static: Iterable[StaticKey],
    observations: Iterable[Observation],
    config: Config,
    include_common: bool = False,
) -> tuple[KeyReport, ...]:
    """A :class:`~dr_ansible.model.KeyReport` per key, sorted by key (FR-17)."""
    docs = {d.path: d for d in documented}
    found: dict[str, list[StaticKey]] = {}
    for key in static:
        found.setdefault(key.path, []).append(key)
    observed = {o.path: o for o in merge_observations(observations)}

    reports = []
    for name in sorted(docs.keys() | found.keys() | observed.keys()):
        if not include_common and name.split(".", 1)[0] in config.common_return_keys:
            continue
        doc = docs.get(name)
        keys = tuple(found.get(name, ()))
        observation = observed.get(name)
        reports.append(
            KeyReport(
                name=name,
                status=_status(doc is not None, bool(keys), observation is not None),
                documented=doc,
                static=keys,
                observed=observation,
            )
        )
    return tuple(reports)


def build_report(
    *,
    module: ModuleInfo,
    docs: DocResult,
    static: Iterable[StaticKey],
    unresolved: Iterable[Unresolved],
    inherits_from: Iterable[str],
    observations: Iterable[Observation],
    config: Config,
    include_common: bool = False,
) -> ModuleReport:
    """Assemble one module's report from its audited docs and gathered evidence."""
    return ModuleReport(
        module=module,
        return_status=docs.status,
        keys=reconcile_keys(
            documented=docs.keys,
            static=static,
            observations=observations,
            config=config,
            include_common=include_common,
        ),
        unresolved=tuple(unresolved),
        inherits_from=tuple(inherits_from),
    )


def _status(documented: bool, static: bool, observed: bool) -> KeyStatus:
    if documented:
        return KeyStatus.OK if static or observed else KeyStatus.STALE
    return KeyStatus.UNDOCUMENTED if static else KeyStatus.TEST_ONLY
