# Research Notes

> **Topic**: How Ansible modules document and produce return values
> **Last Updated**: 2026-10-01

## Summary

A module's return values can come from the module itself, from a same-name
action plugin on the controller, or from both. Documentation is static YAML in
module-level strings (or a sidecar `.yml`) and can be read without importing the
module. dr-ansible therefore needs three independent evidence sources — the
`RETURN` docs, static analysis of module and action-plugin code, and the
module's integration tests — reconciled per key. Source: the requirements doc
([docs/requirements.md](requirements.md)).

## Findings

### Where the code lives

| Content | ansible-core path | Collection path |
|---|---|---|
| Module | `lib/ansible/modules/<name>.py` | `plugins/modules/<name>.py` |
| Action plugin | `lib/ansible/plugins/action/<name>.py` | `plugins/action/<name>.py` |
| Sidecar docs | `lib/ansible/modules/<name>.yml` | `plugins/modules/<name>.yml` |
| Integration tests | `test/integration/targets/<name>/` | `tests/integration/targets/<name>/` |
| Routing / redirects | `lib/ansible/config/ansible_builtin_runtime.yml` | `meta/runtime.yml` |

### Documentation format

- `DOCUMENTATION`, `EXAMPLES`, `RETURN` are YAML strings in module-level variables.
- `ansible.parsing.plugin_docs.read_docstring(filename)` parses them from `.py`
  and sidecar `.yml` without importing the module.
- Verified against ansible-core 2.21.4 (plan task 10):
  - `read_docstring(path, verbose=False, ignore_errors=False)` needs an
    absolute path; with the default `ignore_errors=True` it swallows errors and
    prints an `[ERROR]` line instead of raising.
  - It returns `returndocs=None` both for "no `RETURN`" and for a comment-only
    or empty placeholder, so telling `missing` from `placeholder` needs a
    separate `ast` check for the assignment.
  - Any broken docs variable (e.g. invalid `DOCUMENTATION`) raises
    `AnsibleParserError`, losing a valid `RETURN` too, so the `RETURN` text is
    re-checked on its own with `ansible.parsing.yaml.loader.AnsibleLoader`.
  - Only plain `RETURN = ...` assignments count (the last one wins); an
    annotated `RETURN: str = ...` is ignored, as is a non-literal value.
  - A scalar `RETURN` (e.g. a bare string) is returned as-is, not rejected.
- `RETURN` schema (`validate_modules/schema.py`, `return_schema`): each key needs
  `description`, `returned`, `type`; `type` ∈ `bool, complex, dict, float, int,
  list, raw, str`; optional `sample`, `elements`, `contains`, `version_added`,
  `choices`.

### Module kinds

- **Normal**: builds a `result` dict and calls `module.exit_json(**result)` /
  `fail_json(...)` (e.g. `ping`, `stat`).
- **Virtual**: docs-only module file; the action plugin builds the whole result
  in `ActionModule.run()` (e.g. `fetch`).
- **Hybrid**: action plugin calls `self._execute_module(...)` and merges the
  result (e.g. `copy`); returns can flow through a second action plugin
  (`template` → `copy`).
- **Common returns** (`changed`, `failed`, `msg`, `skipped`, `invocation`,
  `diff`, `warnings`, `deprecations`, `exception`) apply to all modules and are
  excluded by default.

### Known gaps in ansible-core (devel, 2.23.0.dev0)

- 21 modules with no `RETURN`: add_host, assert, async_wrapper, blockinfile,
  debug, dnf, dpkg_selections, expect, fail, fetch, group_by, hostname, iptables,
  known_hosts, meta, package, raw, script, set_fact, set_stats, setup.
- 8 placeholders: assemble, cron, debconf, lineinfile, replace, rpm_key,
  service, subversion.
- Expected to return nothing (exempt): gather_facts, import_playbook,
  import_role, import_tasks, include_role, include_tasks.

## References

- [dr-ansible requirements (live doc)](https://claude.ai/artifact/AkibAyLQKPCs8qEddtGwgB)
- [ansible-core](https://github.com/ansible/ansible) — `lib/ansible/parsing/plugin_docs.py`,
  `test/lib/ansible_test/_util/controller/sanity/validate-modules/validate_modules/schema.py`
