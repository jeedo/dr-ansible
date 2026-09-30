# Test fixtures

Synthetic Ansible trees that dr-ansible's tests analyse. They are **test data**:
ruff and mypy skip this directory, and dr-ansible must only ever parse these
files, never import or run them. `tests/test_fixtures.py` pins down what each
fixture demonstrates, so change both together.

## `core/`: an ansible-core checkout layout

Detected as ansible-core because `lib/ansible/modules/` exists.

| Module | `RETURN` | Demonstrates |
|---|---|---|
| `incremental` | missing | `exit_json(**result)` with the result built by `dict(...)`, `result[k] = ...`, `.update()` and `.setdefault()`; `backup_file` set only under `if changed:`; redirected from `old_incremental` |
| `keywords` | placeholder (`r"""#"""`) | `exit_json(key=...)` keyword arguments of several types; `rc` and `stderr` passed only to `fail_json` (failure-only keys) |
| `helper` | present, out of step | result from a local helper returning a dict; `owner` is undocumented and `legacy_id` is stale |
| `virtual` | missing | a docs-only module; `plugins/action/virtual.py` builds the whole result (like `fetch`) with `result[k] = ...`, `.update()`, `return dict(...)` and a conditional key |
| `hybrid` | present | `plugins/action/hybrid.py` calls `_execute_module` and merges the module's result, then adds `transferred` (like `copy`) |
| `dynamic` | missing | a computed key (`result[f"{prefix}_id"]`) and `**extra` from an unknown caller: both unresolved; `status` is resolvable |
| `sidecar` | in `sidecar.yml` | `DOCUMENTATION` and `RETURN` in a sidecar file instead of the `.py` |
| `nested` | present | a `complex` key with `contains`, returned as a nested dict (like `stat`) |
| `invalid` | invalid YAML | an unclosed flow mapping in `RETURN` |
| `include_tasks` | missing | on the default exempt allowlist, so reported as `exempt` |
| `raises_on_import` | missing | an import trap: raises `RuntimeError` if imported or executed (AC-10) |
| `win_ping` | n/a (`.ps1`) | a PowerShell module with no Python twin: reported as `unsupported` |

`lib/ansible/modules/__init__.py` exists and discovery must skip it.
`lib/ansible/config/ansible_builtin_runtime.yml` redirects `old_incremental` to
`ansible.builtin.incremental`.

Integration targets in `test/integration/targets/`:

- `incremental`: calls the module as `incremental`, `ansible.builtin.incremental`
  and `ansible.builtin.old_incremental`; reads keys with dot and bracket syntax in
  `assert` and `debug`, and uses `when:`.
- `virtual`: a success and an `ignore_errors` failure.
- `hybrid`: tasks under `roles/check_hybrid/tasks/`, plus `failed_when`.
- `keywords`: a `no_log: true` task with a `password` argument (redaction) and a
  failing run that exposes `rc` and `stderr`.

## `collection/`: a source collection

Identified by `galaxy.yml` as `example.widgets`. Module `widget`
(`plugins/modules/widget.py`) has
`RETURN` present; `meta/runtime.yml` redirects `old_widget` to
`example.widgets.widget`; `tests/integration/targets/widget/` uses the FQCN.

## `built_collection/`: an installed collection

Identified by `MANIFEST.json` (no `galaxy.yml`) as `example.gadgets`.
Module `gadget` (`plugins/modules/gadget.py`) has no `RETURN`.
