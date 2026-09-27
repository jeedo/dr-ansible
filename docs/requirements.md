# dr-ansible: Requirements

> Snapshot of the live requirements doc: https://claude.ai/artifact/AkibAyLQKPCs8qEddtGwgB
> **Snapshot taken**: 2026-09-27 (doc rev 86). The live doc is the source of truth; refresh this file when it changes.


## Overview and goals

dr-ansible is a standalone Python command-line tool that finds Ansible modules with missing or incomplete `RETURN` documentation. For each module, it works out which keys the module actually returns and drafts a `RETURN` block that a human finishes.

The problem: in ansible-core, 21 modules in `lib/ansible/modules/` have no `RETURN` block at all (for example `fetch`, `debug`, `setup`, `dnf`, `hostname`), and 8 more have only a placeholder (`RETURN = r"""#"""`). The sanity test only warns about existing modules (`missing-return-legacy`), so the gap persists.

Goals:

1. Audit an ansible-core checkout or a collection and list every module whose `RETURN` is missing, a placeholder, or out of step with the code.
2. Extract the keys each module returns by reading its source, including the action plugin when the module is implemented on the controller.
3. Confirm those keys with evidence from the module's existing integration tests.
4. Draft a `RETURN` skeleton that fills in `type`, `returned` and `sample` from the evidence, and leaves `description` as a `TODO` for a human.

The guiding principle: the tool gathers evidence, and a human writes the explanation. dr-ansible never invents a description and never writes to module files. It prints drafts, and a human pastes them in.

## Background: how modules document and produce returns

A module's return values can come from three places, and dr-ansible must read all of them.

Where the code lives

| Content | ansible-core path | Collection path |
|---|---|---|
| Module (runs on the target) | `lib/ansible/modules/<name>.py` | `plugins/modules/<name>.py` |
| Action plugin (runs on the controller) | `lib/ansible/plugins/action/<name>.py` | `plugins/action/<name>.py` |
| Sidecar docs (instead of in-file strings) | `lib/ansible/modules/<name>.yml` | `plugins/modules/<name>.yml` |
| Integration tests | `test/integration/targets/<name>/` | `tests/integration/targets/<name>/` |
| Module routing and redirects | `lib/ansible/config/ansible_builtin_runtime.yml` | `meta/runtime.yml` |

How documentation is stored

- `DOCUMENTATION`, `EXAMPLES` and `RETURN` are static YAML strings in module-level variables. They are parsed from the source, never by importing the module.
- `ansible.parsing.plugin_docs.read_docstring(filename)` already parses these from `.py` and sidecar `.yml` files. Reuse it rather than writing a new parser.
- The `RETURN` schema is defined in ansible-test's validate-modules (`validate_modules/schema.py`, `return_schema`). Every top-level key needs `description`, `returned` and `type`. `type` is one of `bool`, `complex`, `dict`, `float`, `int`, `list`, `raw` or `str`. The optional keys are `sample`, `elements`, `contains`, `version_added` and `choices`.

How modules produce returns

- A normal module returns through `module.exit_json(**result)` or `module.fail_json(...)`, usually after building a `result` dict step by step (`ping.py`, `stat.py`).
- A virtual module has no code, only docs. The action plugin builds the whole result on the controller. `fetch` is the example: `lib/ansible/modules/fetch.py` is docs only, and `lib/ansible/plugins/action/fetch.py` builds the result with `result.update(...)`, `result[key] = ...` and `return dict(...)` in `ActionModule.run()`.
- A hybrid module has an action plugin that runs the module with `self._execute_module(...)`, then adds to or merges its result. `copy` works this way. `template` hands its result to the `copy` action, so returns can also flow through a second action plugin.
- Common return values such as `changed`, `failed`, `msg`, `skipped`, `invocation` and `diff` apply to all modules. They are normally not documented in each module's `RETURN`, and dr-ansible should exclude them by default.

## Scope and non-goals

In scope:

- Python modules in an ansible-core checkout (`lib/ansible/modules/`) and in any collection (`plugins/modules/`), selected by a path argument.
- Action plugins with the same name as a module, whether the module is virtual or hybrid.
- Mining integration tests that already exist for evidence of return keys.
- Reports, plus draft `RETURN` skeletons for a human to finish.

Out of scope for v1:

- Writing descriptions. That is always a human's job.
- PowerShell and Windows modules (`.ps1`), which have no Python source to analyse. The tool should list them as `unsupported` rather than skip them silently.
- Editing module files. dr-ansible only prints drafts, and a human pastes them in. There is no `--write` option.
- Running arbitrary new tasks against real hosts. Runtime evidence comes only from the module's own integration tests.
- Replacing ansible-test's `validate-modules`. dr-ansible complements it and should agree with it on what counts as missing.

## Functional requirements

dr-ansible runs one pipeline per module. It gathers documented keys, keys found in code and keys seen in tests, then reconciles them into a report and a draft.

_Diagram: dr-ansible pipeline · 3 evidence sources, 2 outputs (see the live doc)._

Each evidence source is independent, so a module with no tests still gets a report from static analysis alone.

### Discovery

- FR-1 Take a path and detect whether it is an ansible-core checkout (`lib/ansible/modules/` exists) or a collection (`galaxy.yml` or `MANIFEST.json`). For a collection, read the namespace and name to build fully qualified names (FQCNs).
- FR-2 List every module `.py` file (skipping `__init__.py`). Pair each one with its sidecar `.yml`, the action plugin of the same name, and the integration target of the same name.
- FR-3 Resolve redirects and aliases from the runtime file, so short names, `ansible.builtin.<name>`, `ansible.legacy.<name>` and collection FQCNs all map to the same module.
- FR-4 Allow filtering with `--module` names or globs.

### Documentation audit

- FR-5 Give each module one `RETURN` status:
   - `missing`: no `RETURN` variable and no sidecar key.
   - `placeholder`: `RETURN` parses to nothing, as with `RETURN = r"""#"""`.
   - `invalid`: the YAML does not parse.
   - `present`: it parses.
- FR-6 Support an allowlist (in the config file) of modules that are expected to return nothing, such as `include_tasks` and `import_role`. Report these as `exempt`, not `missing`.
- FR-7 For `present` modules, check the required fields (`description`, `type`, and `returned` at the top level), and list the documented keys including nested `contains` keys.

### Static return extraction

- FR-8 Analyse source code with Python's `ast` module only. Never import or execute module or plugin code.
- FR-9 In modules, collect keys from:
   - `exit_json(key=...)` keyword arguments.
   - `exit_json(**name)`, tracing `name` back through dict literals, `dict(...)`, `name[key] = ...`, `name.update(...)` and `name.setdefault(...)` within the same function.
   - Local helper functions (one level deep) that return a dict which is later passed to `exit_json`.
   - Keys passed only to `fail_json`, recorded as failure-only. These keys must still appear in the skeleton, with returned: failure.
- FR-10 In action plugins, analyse `ActionModule.run()`: `result[key] = ...`, `result.update(...)` and `return dict(...)`. When the plugin calls `self._execute_module(...)` and merges its result, mark the module as inheriting that module's returns.
- FR-11 For each key, record:
   - The source file and line.
   - An inferred type from the assigned value (a literal, `True`/`False`, an f-string, a list or dict, or `None` meaning unknown).
   - Whether it appears on a success or a failure path.
   - The enclosing condition, as a hint for `returned` (for example, set only inside `if changed:`).
- FR-12 When a key is dynamic (a computed key name, or `**kwargs` from an unknown source), report it as `unresolved` with its location. Do not guess.

### Test mining (evidence)

- FR-13 Static mining (default): parse the YAML in the module's integration target (`tasks/`, `roles/*/tasks/` and playbooks). Find tasks that call the module under any of its names and their `register:` variable. Then collect every `<var>.<key>` or `<var>['<key>']` reference in `assert`, `that`, `when`, `failed_when` and `debug`. These are observed keys.
- FR-14 Runtime mining (opt-in, --run): run `ansible-test integration <target> --docker <image>` with a callback plugin that ships with dr-ansible. The callback records each result from the module under test: its keys, Python types, and truncated values, written to JSON. It runs only with the explicit flag, and in a container by default.
- FR-15 Combine the observations for each key: how many times it was seen, the types seen, whether it appeared on `ok`, `changed` or `failed` results, and one sample value.
- FR-16 Redact sensitive data. Drop values from `no_log` tasks and from keys matching `password`, `token`, `secret` or `key`, and truncate long strings and file contents.

### Reconcile and draft

- FR-17 Give each key one status:
   - `ok`: documented and found.
   - `undocumented`: found in code or tests, but not in `RETURN`.
   - `stale`: documented, but not found anywhere.
   - `test-only`: seen at runtime but not found statically.
- FR-18 Exclude common return values (`changed`, `failed`, `msg`, `skipped`, `invocation`, `diff`, `warnings`, `deprecations`, `exception`) by default. The list is configurable.
- FR-19 Generate a `RETURN` skeleton that passes the validate-modules schema:
   - `description:` set to the fixed marker `DR-ANSIBLE-TODO`.
   - `returned:` inferred as `success`, `changed`, `always`, `failure`, or `when <condition>`.
   - `type:` mapped from Python types (`bool`, `int`, `float`, `str`, `list`, and `dict`, or `complex` when it has `contains`), with `elements` for lists and `contains` for nested dicts.
   - `sample:` from test evidence, falling back to a static literal, and left out if neither exists. Keys returned only on failure are always included.
- FR-20 In merge mode, keep existing documented entries byte for byte and add only the missing keys. Put a YAML comment above each generated key that names its evidence (for example `# from plugins/action/fetch.py:199; seen in 3 tests`).
- FR-21 Never write description text beyond the marker. A human writes every description.

## CLI and output formats

dr-ansible is installed as a console script named `dr-ansible` and has three subcommands.

| Command | Purpose | Key options |
|---|---|---|
| `dr-ansible audit <path>` | List every module with its `RETURN` status and key counts | `--module`, `--status missing,placeholder`, `--format table\|json\|markdown` |
| `dr-ansible returns <path> <module>` | Show each return key for one module, with its evidence and status | `--run`, `--docker <image>`, `--include-common`, `--format` |
| `dr-ansible draft <path> <module>` | Print a draft `RETURN` block | `--merge` (the default), `--full`, `--run`, `--output <file>` |

Exit codes: `0` means nothing is missing, `1` means findings were reported, and `2` means a usage or parse error. This lets `audit` gate CI.

Example audit table output (illustrative values)

```text
MODULE                 RETURN       DOC  CODE  TESTS  UNDOC  STALE
ansible.builtin.fetch  missing        0     7      5      7      0
ansible.builtin.copy   present       14    13      9      0      1
ansible.builtin.cron   placeholder    0     2      1      2      0
```

JSON output. Each module is one object with these fields: `module` (FQCN), `paths` (module, action plugin, sidecar and test target), `return_status`, and `keys[]`. Each entry in `keys[]` has `name`, `status`, `documented`, `static[]` (file, line, inferred type, path, condition), `observed` (count, types, results, sample), and `unresolved[]`. Publish the schema as `schema/report.schema.json` and version it.

Example draft output for fetch

```yaml
RETURN = r"""
dest:
    # from lib/ansible/plugins/action/fetch.py:199,208; seen in 4 tests
    description: DR-ANSIBLE-TODO
    returned: success
    type: str
    sample: /tmp/fetched/host.example.com/tmp/somefile
remote_checksum:
    # from lib/ansible/plugins/action/fetch.py:199; seen in 2 tests
    description: DR-ANSIBLE-TODO
    returned: changed
    type: str
    sample: 6e642bb8dd5c2e027bf21dd923337cbb4214f827
"""
```

## Non-functional requirements

- NFR-1 Language and runtime: Python 3.13 or later, matching the current ansible-core devel (`requires-python = ">=3.13"`, version 2.23.0.dev0). Use type hints throughout.
- NFR-2 Packaging: a standalone package in https://github.com/jeedo/dr-ansible, with PEP 621 metadata in `pyproject.toml`, a standard PEP 517 build backend (for example `hatchling`), and a `dr-ansible` console script. uv is the primary workflow, and plain pip still works.
   - Development: run `uv sync` to create the environment. Commit `uv.lock` so dependency versions are reproducible. Put dev tools (`pytest`, `ruff`, `mypy`) in `[dependency-groups]`, and run them with `uv run pytest` and so on.
   - Run from a checkout: `uv run dr-ansible audit <path>`.
   - Install as a tool: `uv tool install git+https://github.com/jeedo/dr-ansible`, or run it once without installing with `uvx --from git+https://github.com/jeedo/dr-ansible dr-ansible audit <path>`.
   - Without uv: `pip install -e .` for development, and `pipx install git+https://github.com/jeedo/dr-ansible` as a tool.
   - Python version: set `requires-python = ">=3.13"` (NFR-1), and pin the development interpreter with a `.python-version` file that uv reads.
- NFR-3 Dependencies: keep them minimal.
   - Required: `ansible-core`, for `ansible.parsing.plugin_docs` and YAML loading.
   - Required: `PyYAML`, for reading test YAML.
   - Optional: `rich`, for table output.
   - `ansible-test` is invoked as a subprocess only for `--run`, never imported.
- NFR-4 Licensing: GPL-3.0-or-later, because the tool imports ansible-core. Every dependency must be GPLv3-compatible.
- NFR-5 Safety:
   - Default operations are read-only and static.
   - Nothing is executed without `--run`, and `--run` uses a container unless `--local` is also given.
   - Module files are never modified. Drafts and reports go to stdout, or to a file named with `--output`.
- NFR-6 Determinism: the same input produces the same output. Sort modules and keys, and choose samples by a stable rule (the first observation in file order).
- NFR-7 Performance: a static `audit` of all of ansible-core's roughly 70 modules finishes in under 10 seconds on a laptop. Runtime mining is expected to take minutes per target.
- NFR-8 Robustness: if one module fails to parse, report it as `error` with the reason, and keep going with the rest.
- NFR-9 Configuration: optional `dr-ansible.toml` (or `[tool.dr-ansible]` in `pyproject.toml`) for the allowlist of exempt modules, the list of common return keys, redaction patterns, and the default Docker image.

## Acceptance criteria and test fixtures

The tool is done when every check below passes against a pinned ansible-core devel checkout.

- [ ] AC-1 `dr-ansible audit` on ansible-core reports these 21 modules as `missing`: add_host, assert, async_wrapper, blockinfile, debug, dnf, dpkg_selections, expect, fail, fetch, group_by, hostname, iptables, known_hosts, meta, package, raw, script, set_fact, set_stats, setup.
- [ ] AC-2 It reports these 8 modules as `placeholder`: assemble, cron, debconf, lineinfile, replace, rpm_key, service, subversion.
- [ ] AC-3 With the default allowlist, it reports gather_facts, import_playbook, import_role, import_tasks, include_role and include_tasks as `exempt`.
- [ ] AC-4 `dr-ansible returns . fetch` finds `file`, `dest`, `checksum`, `remote_checksum`, `md5sum` and `remote_md5sum` in `lib/ansible/plugins/action/fetch.py`, with the correct line numbers. It excludes `changed`, `failed` and `msg` unless `--include-common` is given.
- [ ] AC-5 Static test mining for `fetch` links registered variables in `test/integration/targets/fetch/` (for example `fetch_missing_nofail`) to observed keys.
- [ ] AC-6 `dr-ansible returns . stat` shows `stat` as `ok`, and the nested keys under its `contains` agree with the documentation.
- [ ] AC-7 For `copy` (a hybrid), the report shows that the action plugin inherits returns from the `copy` module.
- [ ] AC-8 For every module in AC-1, `dr-ansible draft` output passes `ansible-test sanity --test validate-modules` when pasted into the module (the placeholder descriptions are the only content a human needs to replace).
- [ ] AC-9 `--merge` leaves existing `RETURN` entries unchanged, byte for byte.
- [ ] AC-10 No module or plugin code is imported or executed unless `--run` is given. A unit test proves this by auditing a fixture module that raises when imported.
- [ ] AC-11 `--run` against the `ping` target records `ping` with type `str` and sample `pong`.
- [ ] AC-12 Values from `no_log` tasks and from keys matching the redaction patterns never appear in output.

Fixtures for dr-ansible's own tests. The tool's test suite needs small synthetic modules, one for each pattern:

- `exit_json(**result)` with a result built up incrementally.
- `exit_json(key=...)` keyword arguments.
- A helper function that returns a dict.
- A virtual module with an action plugin.
- A hybrid action plugin that uses `_execute_module`.
- A dynamic key that must be reported as `unresolved`.
- A sidecar `.yml` docs file.
- A collection layout with `galaxy.yml`.

## Decisions and future work

Decisions (all resolved):

- [x] Should `--write` exist in v1, or should dr-ansible only print drafts for a human to paste in? dr-ansible only print drafts for human to paste in
- [x] Should the skeleton list keys a module returns only when the task itself fails, such as `keys passed only to fail_json`, or leave them out? Decided: list them, with returned: failure (see FR-9 and FR-19).
- [x] Is the `DR-ANSIBLE-TODO` marker acceptable, or should descriptions be left empty so that validate-modules fails until a human fills them in? this is ok
- [x] Where does the tool live: a new GitHub repo named `dr-ansible`, or a folder in an existing repo? in a new github repo https://github.com/jeedo/dr-ansible

Future work, out of scope for v1:

- PowerShell modules, by parsing `Exit-Json` and `$module.Result` in `.ps1` files.
- A `--changelog` option that also prints a `changelogs/fragments/` entry for each documented module.
- Checking `DOCUMENTATION` options against `argument_spec` in the same style.
- A pre-commit hook that runs `dr-ansible audit --status missing` on changed modules.
