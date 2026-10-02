# Architecture: dr-ansible

> **Status**: Approved
> **Last Updated**: 2026-09-27
> **Requirements**: [docs/requirements.md](requirements.md) (snapshot of the [live requirements doc](https://claude.ai/artifact/AkibAyLQKPCs8qEddtGwgB))

## Overview & Goals

dr-ansible is a standalone, read-only Python CLI that finds Ansible modules with
missing, placeholder or out-of-date `RETURN` documentation, works out which keys
each module actually returns, and prints a draft `RETURN` block for a human to
finish. It gathers evidence; a human writes every description.

**Problem Statement**: In ansible-core, 21 modules have no `RETURN` block and 8
more have only a placeholder. `validate-modules` only warns for legacy modules
(`missing-return-legacy`), so the gap persists. Writing the block by hand means
reading the module, its action plugin and its tests to discover the keys.

**Success Criteria**:
- `audit` reproduces the AC-1/AC-2/AC-3 module lists on a pinned ansible-core devel checkout.
- `returns` finds the documented and undocumented keys for `fetch`, `stat` and `copy` as in AC-4 to AC-7.
- `draft` output for every AC-1 module passes `ansible-test sanity --test validate-modules` once pasted in (AC-8).
- No module or plugin code is imported or executed unless `--run` is given (AC-10).
- A full static `audit` of ansible-core finishes in under 10 seconds (NFR-7).

## Tech Stack

| Layer | Technology | Rationale |
|-------|-----------|-----------|
| Language | Python ≥ 3.13, fully type-hinted | Matches ansible-core devel (NFR-1); `.python-version` pins 3.13 for uv |
| Packaging | PEP 621 `pyproject.toml`, `uv_build` backend, `src/` layout | uv's own PEP 517 backend; still builds and installs with plain pip (NFR-2). Bundles the callback plugin as package data |
| Env / deps | `uv` with committed `uv.lock`; dev tools in `[dependency-groups]` | Reproducible; project convention in `CLAUDE.md` |
| CLI | `click` | Declarative subcommands, options and help; `CliRunner` for CLI tests. BSD-3-Clause, GPLv3-compatible (NFR-4) |
| Doc parsing | `ansible-core>=2.21.4` (latest release; exact version pinned in `uv.lock`) → `ansible.parsing.plugin_docs.read_docstring` | Reuse Ansible's own parser for `.py` and sidecar `.yml` docs. The installed release is a library only; the ansible-core checkout being audited is read as data and never imported |
| Source analysis | `ast` (stdlib) | Never import or execute analysed code (FR-8) |
| Test YAML | `PyYAML` (`safe_load`) | Read integration target tasks (FR-13) |
| Config | `tomllib` (stdlib) | `dr-ansible.toml` or `[tool.dr-ansible]` (NFR-9) |
| Table output | `rich` as optional extra `dr-ansible[rich]`, plain-text fallback | Keep required deps minimal (NFR-3) |
| Runtime mining | `ansible-test integration --docker` via `subprocess`, plus a bundled callback plugin | Opt-in only (FR-14, NFR-5) |
| Dev tools | `pytest`, `ruff`, `mypy` (strict), `jsonschema` (tests only) | Template workflow; schema test for JSON output |
| Distribution | PyPI (`dr-ansible`), released from `v*` tags by a GitHub Actions workflow using trusted publishing | `pip install dr-ansible` / `uvx dr-ansible`; no stored API tokens; attestations for provenance |
| Dependency updates | Dependabot (`.github/dependabot.yml` + security updates) | Weekly grouped minor/patch PRs for `uv` and `github-actions`, separate PRs for majors, security fix PRs; same setup as jeedo/oneshot |
| License | GPL-3.0-or-later | Imports ansible-core (NFR-4) |

## System Components

The tool runs one pipeline per module: discover → three independent evidence
sources → reconcile → report or draft. Each evidence source can fail or be empty
without stopping the others (NFR-8).

```
                    ┌──────────────┐
  <path> ─────────▶ │  discovery   │── ModuleInfo[] (sorted, filtered)
                    └──────┬───────┘
          ┌────────────────┼─────────────────┐
          ▼                ▼                 ▼
   ┌────────────┐   ┌─────────────┐   ┌──────────────┐
   │ docs_audit │   │   static    │   │    mining    │
   │ (RETURN)   │   │ (ast: module│   │ (static YAML │
   │            │   │ + action)   │   │  / --run)    │
   └─────┬──────┘   └──────┬──────┘   └──────┬───────┘
         └─────────────────┼─────────────────┘
                           ▼
                    ┌─────────────┐
                    │  reconcile  │── ModuleReport
                    └──────┬──────┘
               ┌───────────┴───────────┐
               ▼                       ▼
        ┌─────────────┐         ┌─────────────┐
        │   report    │         │    draft    │
        │ table/json/ │         │ RETURN YAML │
        │  markdown   │         │ (stdout /   │
        └─────────────┘         │  --output)  │
                                └─────────────┘
```

Package layout (`src/dr_ansible/`):

- **`cli.py`** — `click` group with `audit`, `returns` and `draft` subcommands;
  maps outcomes to exit codes `0` (clean), `1` (findings), `2` (usage/parse error).
- **`config.py`** — loads `dr-ansible.toml`, else `[tool.dr-ansible]` in the target's
  `pyproject.toml`, over built-in defaults: exempt allowlist (AC-3 list),
  common return keys (FR-18), redaction patterns, default Docker image.
- **`discovery.py`** (FR-1 to FR-4) — detects the layout (ansible-core if
  `lib/ansible/modules/` exists; collection if `galaxy.yml` or `MANIFEST.json`),
  reads the collection namespace/name, lists `*.py` modules (skipping
  `__init__.py`) and `.ps1` modules (marked `unsupported`), pairs each module with
  its sidecar `.yml`, same-name action plugin and same-name integration target,
  and resolves names and redirects from `ansible_builtin_runtime.yml` /
  `meta/runtime.yml` so that short names, `ansible.builtin.*`, `ansible.legacy.*`
  and FQCNs all map to one module. Applies `--module` names/globs.
- **`docs_audit.py`** (FR-5 to FR-7) — an `ast` pass decides whether a `RETURN`
  assignment (or sidecar key) exists at all; `read_docstring` then parses it.
  Status: `missing` (no variable/key), `placeholder` (parses to empty),
  `invalid` (YAML error), `present`; config allowlist overrides to `exempt`.
  For `present`, checks top-level `description`/`type`/`returned` and flattens
  documented keys, including nested `contains`, into dotted paths.
- **`static/`** (FR-8 to FR-12) — pure `ast` analysis, never imports:
  - `module_analyzer.py` — finds `exit_json`/`fail_json` calls; collects keyword
    keys; traces `**name` back through dict literals, `dict(...)`,
    `name[k] = ...`, `.update(...)` and `.setdefault(...)` within the function;
    follows local helpers one level deep. Keys seen only in `fail_json` are
    failure-only.
  - `action_analyzer.py` — analyses `ActionModule.run()` for `result[k] = ...`,
    `result.update(...)` and `return dict(...)`; detects
    `self._execute_module(...)` merged into the result and records
    `inherits_from = <module>` (AC-7), including delegation to another action
    plugin (`template` → `copy`).
  - `infer.py` — literal → type (`bool`, `int`, `float`, `str` incl. f-strings,
    `list` with element type, `dict` with nested keys, `None` = unknown);
    records the enclosing `if` test as source text for the `returned` hint.
  - Computed key names and `**kwargs` from unknown sources become `unresolved`
    entries with file and line; nothing is guessed.
- **`mining/`** (FR-13 to FR-16)
  - `static_miner.py` — loads YAML under the integration target (`tasks/`,
    `roles/*/tasks/`, playbooks), finds tasks invoking the module under any of
    its names, records their `register:` variable, and extracts
    `<var>.<key>` / `<var>['<key>']` references from `assert`/`that`, `when`,
    `failed_when` and `debug` expressions (regex over Jinja expression strings;
    no Jinja evaluation).
  - `runtime.py` — only with `--run`: runs
    `ansible-test integration <target> --docker <image>` (or `--local` if given)
    as a subprocess, with the bundled callback enabled, then reads its JSON.
    ansible-test replaces the environment, config file and enabled callbacks of
    the Ansible processes it starts, so the project is first copied to a
    temporary directory (the analysed tree is never touched, NFR-5); the copy
    gets a self-enabling copy of the callback and an `integration.cfg` that
    names its plugin directory and output file relative to `$JUNIT_OUTPUT_DIR`,
    which ansible-test sets to its results directory, locally and in the
    container alike. The recording is read back from that results directory.
  - `callback/dr_ansible_recorder.py` — Ansible callback plugin shipped as package
    data; records, for each result of the module under test, its keys, Python
    types, result state (`ok`/`changed`/`failed`) and truncated values. Skips
    values from `no_log` tasks.
  - `redact.py` — drops values for keys matching the redaction patterns
    (`password`, `token`, `secret`, `key` by default) and truncates long strings,
    applied both in the callback and again before output (AC-12).
- **`reconcile.py`** (FR-17, FR-18) — joins documented, static and observed keys
  per dotted path into one `KeyReport` each with status `ok`, `undocumented`,
  `stale` or `test-only`; drops common return keys unless `--include-common`.
- **`draft.py`** (FR-19 to FR-21) — builds the skeleton and emits YAML with a
  small deterministic hand-written emitter (so it can place `# from …` evidence
  comments and control key order). `description: DR-ANSIBLE-TODO`; `returned`
  from evidence (`always` / `success` / `changed` / `failure` / `when <cond>`);
  `type` mapped from Python types (`dict` with `contains` → `complex`,
  `elements` for lists); `sample` from the first test observation, else a static
  literal, else omitted. **Merge mode** (default) copies the existing `RETURN`
  text verbatim and appends only missing top-level keys after it; missing nested
  keys under an existing entry are listed as a YAML comment rather than edited
  in, so existing bytes are never touched (AC-9). `--full` regenerates everything.
- **`report/`** — `table.py` (rich if installed, else aligned plain text),
  `json_report.py` (validated against `schema/report.schema.json`, versioned via a
  `schema_version` field), `markdown.py`. All output is sorted by module FQCN and
  key path (NFR-6).

Safety boundaries (NFR-5): no code path writes to the analysed tree; the only
file write is `--output <file>`; the only process spawn is in `mining/runtime.py`,
reached only via `--run`.

## Data Model / API

Core types (frozen `dataclasses` in `dr_ansible/model.py`):

| Type | Fields |
|------|--------|
| `ModuleInfo` | `name`, `fqcn`, `aliases: list[str]`, `language: python\|powershell`, `module_path`, `sidecar_path?`, `action_path?`, `test_target?` |
| `DocResult` | `status: missing\|placeholder\|invalid\|present\|exempt`, `keys: dict[path, DocumentedKey]`, `raw_text?` (for merge), `problems: list[str]` |
| `DocumentedKey` | `path`, `type?`, `returned?`, `has_description: bool`, `elements?` |
| `StaticKey` | `path`, `file`, `line`, `inferred_type?`, `outcome: success\|failure`, `condition?` |
| `Unresolved` | `file`, `line`, `reason` |
| `Observation` | `path`, `count`, `types: set[str]`, `results: set[ok\|changed\|failed]`, `sample?`, `sources: list[file:line]` |
| `KeyReport` | `name`, `status: ok\|undocumented\|stale\|test-only`, `documented?`, `static: list[StaticKey]`, `observed?` |
| `ModuleReport` | `module` (FQCN), `paths`, `return_status` (adds `error`, `unsupported`), `inherits_from?`, `keys: list[KeyReport]`, `unresolved: list[Unresolved]`, `error?` |

CLI (console script `dr-ansible`):

| Command | Purpose | Options |
|---------|---------|---------|
| `dr-ansible audit <path>` | Every module with its `RETURN` status and key counts | `--module`, `--status`, `--format table\|json\|markdown` |
| `dr-ansible returns <path> <module>` | Every return key for one module, with evidence and status | `--run`, `--docker <image>`, `--local`, `--include-common`, `--format` |
| `dr-ansible draft <path> <module>` | Print a draft `RETURN` block | `--merge` (default), `--full`, `--run`, `--output <file>` |

Exit codes: `0` nothing missing, `1` findings reported, `2` usage or parse error.

JSON report: one object per module with `module`, `paths`, `return_status`,
`keys[]` (`name`, `status`, `documented`, `static[]`, `observed`) and
`unresolved[]`, plus a top-level `schema_version`. The schema lives at
`schema/report.schema.json`.

Testing strategy:

- **Unit tests** against small synthetic fixtures under `tests/fixtures/`
  (one per pattern listed in the requirements, plus a module that raises on
  import for AC-10).
- **Acceptance tests** (`pytest -m acceptance`) against a pinned ansible-core
  devel checkout fetched into a cache directory (audited as data; the tool itself
  runs on the released ansible-core from PyPI); AC-8 (validate-modules) and
  AC-11 (`--run` on `ping`, needs Docker) are marked separately so default CI
  stays fast.

## Decisions Beyond the Requirements

Agreed during architecture review (2026-09-27); these refine or replace details
in the requirements snapshot:

- **License**: GPL-3.0-or-later (replaces the repo's initial Apache-2.0 file).
- **ansible-core**: depend on the latest release from PyPI (2.21.4), not the
  devel checkout; audited checkouts are data only.
- **CLI framework**: `click` (an extra runtime dependency beyond NFR-3's list).
  Click already exits with `2` on usage errors, matching the exit-code contract.
- **Build backend**: `uv_build` (the requirements suggested `hatchling` as an example).
- **Dependency maintenance** (added 2026-09-28): Dependabot version and security
  updates, gated by CI and human review (no auto-merge).
- **Distribution** (added 2026-09-28): published to PyPI as `dr-ansible` in
  addition to the git installs in NFR-2; releases use trusted publishing via
  GitHub Actions, go to TestPyPI first, and need a human approval before PyPI.

## Research & References

See [docs/research.md](research.md) for how Ansible stores and produces return
values (paths, `RETURN` schema, module kinds). Requirements:
[docs/requirements.md](requirements.md).
