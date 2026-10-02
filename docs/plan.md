# Implementation Plan

> Generated from [Architecture](architecture.md)
> **Last Updated**: 2026-09-28
> **Status**: Approved

Requirement IDs (FR/NFR/AC) refer to [requirements.md](requirements.md).

## Phase 1: Setup & Scaffolding

- [x] 1. Initialise the package with uv: `pyproject.toml` (PEP 621, `uv_build` backend, `requires-python = ">=3.13"`, GPL-3.0-or-later), `src/dr_ansible/` layout, `.python-version` (3.13), `dr-ansible` console script pointing at a stub `click` group; commit `uv.lock` (NFR-1, NFR-2)
- [x] 2. Add the remaining dependencies with `uv add`: `ansible-core>=2.21.4`, `PyYAML`; optional extra `rich`; dev group `ruff`, `mypy` (strict), `jsonschema`, `types-PyYAML` (`click` and `pytest` were added in task 1) (NFR-3)
- [x] 3. Configure ruff, mypy and pytest (markers `acceptance` and `runtime`) in `pyproject.toml`; anchor `.gitignore`'s `lib/` to `/lib/` so fixture trees are tracked
- [x] 4. Update CI (`.github/workflows/ci.yml`) to Python 3.13 and `uv sync`; run ruff, ruff format check, mypy and unit tests on push and PR
- [x] 5. Define the core dataclasses in `model.py` (`ModuleInfo`, `DocResult`, `DocumentedKey`, `StaticKey`, `Unresolved`, `Observation`, `KeyReport`, `ModuleReport`)
- [x] 6. Build the synthetic test fixtures: an ansible-core-style tree and a collection with `galaxy.yml`, covering incremental `exit_json(**result)`, keyword `exit_json`, helper returning a dict, virtual module + action plugin, hybrid `_execute_module` action plugin, dynamic key, sidecar `.yml`, a `.ps1` module, and a module that raises on import

## Phase 2: Core Domain

- [x] 7. `config.py`: load `dr-ansible.toml` or `[tool.dr-ansible]` over built-in defaults (exempt allowlist, common return keys, redaction patterns, Docker image) (NFR-9, FR-6, FR-18)
- [x] 8. `discovery.py`: detect ansible-core vs collection layout and read namespace/name for FQCNs; list `.py` modules (skip `__init__.py`) and `.ps1` as `unsupported`; pair sidecar, action plugin and integration target (FR-1, FR-2)
- [x] 9. `discovery.py`: resolve names, aliases and redirects from `ansible_builtin_runtime.yml` / `meta/runtime.yml`; `--module` name/glob filtering; deterministic sort (FR-3, FR-4, NFR-6)
- [x] 10. `docs_audit.py`: `ast` check for a `RETURN` assignment or sidecar key plus `read_docstring` parsing; classify `missing` / `placeholder` / `invalid` / `present` / `exempt`; verify how `read_docstring` treats placeholders (FR-5, FR-6)
- [x] 11. `docs_audit.py`: required-field checks and flattening of documented keys, including nested `contains`, into dotted paths (FR-7)
- [x] 12. `static/infer.py`: type inference from AST values (literals, bools, f-strings, lists with element types, dicts with nested keys, unknown) and enclosing-condition capture (FR-11)
- [x] 13. `static/module_analyzer.py`: `exit_json` keyword keys and `**name` tracing through dict literals, `dict(...)`, subscript assignment, `.update()` and `.setdefault()` within a function (FR-8, FR-9)
- [x] 14. `static/module_analyzer.py`: one-level local helper resolution, failure-only keys from `fail_json`, and `unresolved` reporting for dynamic keys (FR-9, FR-12)
- [x] 15. `static/action_analyzer.py`: analyse `ActionModule.run()` (`result[k] = ...`, `.update()`, `return dict(...)`) and detect `_execute_module` inheritance, including delegation to another action plugin (FR-10, AC-7)
- [x] 16. `mining/static_miner.py`: parse integration target YAML, match tasks by any module name, track `register:` variables, extract `<var>.<key>` / `<var>['<key>']` from `assert`/`that`, `when`, `failed_when` and `debug` (FR-13, AC-5)
- [x] 17. `mining/redact.py`: drop `no_log` values and values of keys matching redaction patterns; truncate long strings (FR-16, AC-12)
- [x] 18. `reconcile.py`: merge documented, static and observed evidence per key into `ok` / `undocumented` / `stale` / `test-only`; exclude common keys unless requested (FR-15, FR-17, FR-18)
- [x] 19. `draft.py`: deterministic YAML emitter for the full skeleton with `DR-ANSIBLE-TODO` descriptions, inferred `returned` / `type` / `elements` / `contains` / `sample`, and evidence comments (FR-19, FR-21)
- [x] 20. `draft.py`: merge mode that copies existing `RETURN` text byte for byte and appends only missing top-level keys, listing missing nested keys as comments (FR-20, AC-9)

## Phase 3: API / Interface

- [x] 21. `report/`: table output (rich if installed, plain-text fallback), markdown output, and JSON output with `schema_version`
- [x] 22. Write `schema/report.schema.json` and validate JSON output against it in tests
- [x] 23. `cli.py`: `audit` command with `--module`, `--status`, `--format`, and exit codes 0/1/2; per-module failures reported as `error` without stopping the run (NFR-8)
- [x] 24. `cli.py`: `returns` command with `--include-common` and `--format`
- [x] 25. `cli.py`: `draft` command with `--merge` (default), `--full` and `--output <file>` (NFR-5)
- [x] 26. `mining/callback/dr_ansible_recorder.py`: callback plugin recording keys, types, result state and truncated values for the module under test, skipping `no_log` results (FR-14)
- [x] 27. `mining/runtime.py` and `--run` / `--docker` / `--local` on `returns` and `draft`: run `ansible-test integration` via subprocess with the callback enabled and merge observations (FR-14, NFR-5)

## Phase 4: Testing & QA

- [x] 28. Safety test: audit the raise-on-import fixture and assert nothing is imported or executed without `--run` (AC-10)
- [x] 29. Acceptance harness: fetch a pinned ansible-core devel checkout into a cache directory; tests marked `acceptance`
- [x] 30. Acceptance tests for `audit`: AC-1 missing list, AC-2 placeholder list, AC-3 exempt list
- [x] 31. Acceptance tests for `returns`: `fetch` keys and line numbers (AC-4), `fetch` test mining (AC-5), `stat` nested keys (AC-6), `copy` inheritance (AC-7)
- [x] 32. Acceptance test for drafts: paste each AC-1 draft into the checkout and run `ansible-test sanity --test validate-modules` (AC-8)
- [ ] 33. Runtime acceptance test (marked `runtime`, needs Docker): `--run` on `ping` records `ping` as `str` with sample `pong` (AC-11); redaction check (AC-12)
- [ ] 34. Performance check: full static `audit` of ansible-core in under 10 seconds (NFR-7); determinism check that two runs produce identical output (NFR-6)

## Phase 5: CI/CD & Deployment

- [ ] 35. Add a CI job for acceptance tests (cached ansible-core checkout, validate-modules) and a manual/scheduled job for runtime tests
- [ ] 36. Automated dependency updates with Dependabot (`.github/dependabot.yml`), as in jeedo/oneshot: weekly version updates for the `uv` (`pyproject.toml` + `uv.lock`) and `github-actions` ecosystems, grouped per ecosystem for minor + patch only so major bumps get their own PR for manual review; Dependabot security updates enabled in the repository settings; Dependabot PRs run the full CI suite, `main` branch protection requires every check to pass, and there is no auto-merge — a human reviews and approves
- [ ] 37. PyPI-ready metadata: project URLs, keywords, classifiers (Python 3.13, GPLv3+, console, Ansible framework), README as the long description; version bumped with `uv version`; a test that `uv build` produces a wheel and sdist that pass `twine check --strict`
- [ ] 38. Publish to PyPI as `dr-ansible`: a release workflow (`.github/workflows/release.yml`) triggered by a `v*` tag that verifies the tag matches the package version, runs the full CI suite, builds with `uv build`, publishes to TestPyPI and then PyPI via trusted publishing (OIDC, no API tokens) with `pypa/gh-action-pypi-publish` (PEP 740 attestations), gates the PyPI step behind a protected `pypi` GitHub environment that needs a human approval, and attaches the built files to a GitHub release. One-time manual setup: register the pending trusted publisher on PyPI and TestPyPI, and create the `pypi` / `testpypi` environments
- [ ] 39. Verify install paths from PyPI and from git: `pip install dr-ansible`, `pipx install dr-ansible`, `uv tool install dr-ansible`, `uvx dr-ansible`, plus `uv tool install git+…` and `pip install -e .` for development (NFR-2)
- [ ] 40. Write user documentation in `README.md`: install (PyPI first, then git), commands, configuration, exit codes, and using `audit` as a CI gate
