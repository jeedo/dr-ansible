"""Run every analysis stage for one module and assemble its report.

The stages are independent (see ``docs/architecture.md``): the documentation
audit, static analysis of the module and its action plugin, and mining of the
integration target. A module that cannot be analysed becomes an ``error``
report with the reason, so one bad file never stops a run (NFR-8).
"""

from dataclasses import dataclass

from dr_ansible.config import Config
from dr_ansible.docs_audit import DocsAuditError, audit_docs
from dr_ansible.mining.static_miner import mine_target
from dr_ansible.model import (
    DocResult,
    KeyStatus,
    Language,
    ModuleInfo,
    ModuleReport,
    Observation,
    ReturnStatus,
    StaticKey,
    Unresolved,
)
from dr_ansible.reconcile import build_report
from dr_ansible.static.action_analyzer import analyze_action
from dr_ansible.static.module_analyzer import analyze_module
from dr_ansible.static.tracer import AnalysisError

#: Return statuses that are findings by themselves.
_FINDING_STATUSES = frozenset(
    {
        ReturnStatus.MISSING,
        ReturnStatus.PLACEHOLDER,
        ReturnStatus.INVALID,
        ReturnStatus.ERROR,
    }
)
#: Return statuses that are never findings, whatever their keys.
_SETTLED_STATUSES = frozenset({ReturnStatus.EXEMPT, ReturnStatus.UNSUPPORTED})
_FINDING_KEYS = frozenset(
    {KeyStatus.UNDOCUMENTED, KeyStatus.TEST_ONLY, KeyStatus.STALE}
)


@dataclass(frozen=True, slots=True)
class ModuleResult:
    """A module's report, and its parsed docs when they could be read."""

    report: ModuleReport
    docs: DocResult | None = None


def analyze(
    module: ModuleInfo,
    config: Config,
    *,
    include_common: bool = False,
    observations: tuple[Observation, ...] = (),
) -> ModuleResult:
    """Analyse ``module`` from its files alone; nothing is imported or run.

    ``observations`` adds evidence from elsewhere, such as a runtime run, to
    what static mining of the integration target finds.
    """
    if module.language is not Language.PYTHON:
        return ModuleResult(
            ModuleReport(module=module, return_status=ReturnStatus.UNSUPPORTED)
        )
    try:
        return _analyze(module, config, include_common, observations)
    except (DocsAuditError, AnalysisError) as exc:
        reason = str(exc)
    except Exception as exc:  # NFR-8: report it and keep going
        reason = f"internal error: {type(exc).__name__}: {exc}"
    return ModuleResult(
        ModuleReport(module=module, return_status=ReturnStatus.ERROR, error=reason)
    )


def _analyze(
    module: ModuleInfo,
    config: Config,
    include_common: bool,
    observations: tuple[Observation, ...],
) -> ModuleResult:
    docs = audit_docs(module, config)
    analysis = analyze_module(module.module_path)
    static: list[StaticKey] = list(analysis.keys)
    unresolved: list[Unresolved] = list(analysis.unresolved)
    inherits: list[str] = []
    if module.action_path is not None:
        action = analyze_action(module.action_path, module.fqcn)
        static += action.keys
        unresolved += action.unresolved
        inherits += action.inherits_from
    report = build_report(
        module=module,
        docs=docs,
        static=static,
        unresolved=unresolved,
        inherits_from=inherits,
        observations=[*mine_target(module).observations, *observations],
        config=config,
        include_common=include_common,
    )
    return ModuleResult(report, docs)


def has_findings(report: ModuleReport) -> bool:
    """Whether ``report`` needs attention: the meaning of exit code 1.

    A missing, placeholder or invalid ``RETURN``, an analysis error, or any
    key that is undocumented, test-only or stale. Exempt and unsupported
    modules never have findings.
    """
    if report.return_status in _SETTLED_STATUSES:
        return False
    if report.return_status in _FINDING_STATUSES:
        return True
    return any(key.status in _FINDING_KEYS for key in report.keys)
