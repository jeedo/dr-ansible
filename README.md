# dr-ansible

Documentation help for Ansible: find modules with missing or incomplete
`RETURN` documentation, work out which keys they actually return, and print a
draft `RETURN` block for a human to finish.

dr-ansible reads an ansible-core checkout or a collection **without importing or
running any of its code**. It compares three kinds of evidence for every return
key:

- **the docs**: the module's `RETURN` block;
- **the code**: where the module or its action plugin sets the key;
- **the tests**: where the module's integration tests read the key, or, with
  `--run`, what the module really returned when they ran.

> **Status**: alpha. Requires Python 3.13 or later.

## Install

From PyPI:

```bash
uv tool install dr-ansible     # or: pipx install dr-ansible / pip install dr-ansible
uvx dr-ansible --help          # run once without installing
```

From git, for the latest `main`:

```bash
uv tool install git+https://github.com/jeedo/dr-ansible
uvx --from git+https://github.com/jeedo/dr-ansible dr-ansible --help
```

For development, from a clone:

```bash
uv sync --group dev
uv run dr-ansible --help
```

Optional: install the `rich` extra (`dr-ansible[rich]`) for nicer tables.

## Quick start

Point dr-ansible at an ansible-core checkout (anything with
`lib/ansible/modules/`) or a collection (anything with `galaxy.yml` or
`MANIFEST.json`):

```console
$ dr-ansible audit ~/src/ansible --module fetch,copy,cron,stat,include_tasks
MODULE                         RETURN       DOC  CODE  TESTS  UNDOC  STALE
ansible.builtin.copy           present       12     9      5      5      6
ansible.builtin.cron           placeholder    0     8      0      8      0
ansible.builtin.fetch          missing        0     6      4      6      0
ansible.builtin.include_tasks  exempt         0     0      0      0      0
ansible.builtin.stat           present       45    57      4     12      0
```

Look at one module's keys and where each one comes from:

```console
$ dr-ansible returns ~/src/ansible fetch
ansible.builtin.fetch: RETURN missing
inherits returns from: ansible.legacy.slurp

KEY              STATUS        DOC  CODE                                                    TESTS
checksum         undocumented  -    lib/ansible/plugins/action/fetch.py:197,200,208             3
dest             undocumented  -    lib/ansible/plugins/action/fetch.py:196,199,208             4
file             undocumented  -    lib/ansible/plugins/action/fetch.py:84,118,196,199,208      2
md5sum           undocumented  -    lib/ansible/plugins/action/fetch.py:195,199,208             -
remote_checksum  undocumented  -    lib/ansible/plugins/action/fetch.py:197,201                 2
remote_md5sum    undocumented  -    lib/ansible/plugins/action/fetch.py:196,200                 -
```

Then draft its `RETURN` block:

```console
$ dr-ansible draft ~/src/ansible fetch
# dr-ansible draft for ansible.builtin.fetch: replace every DR-ANSIBLE-TODO with a description.
# no existing RETURN to merge into: this is a full draft
RETURN = r"""
checksum:
    # from lib/ansible/plugins/action/fetch.py:197,200,208; seen in 3 tests
    description: DR-ANSIBLE-TODO
    returned: success
    type: raw
...
```

Paste the draft into the module and replace every `DR-ANSIBLE-TODO`: dr-ansible
never writes a description, a human does. Drafts pass ansible-test's
`validate-modules` sanity test as they are.

## Commands

### `dr-ansible audit PATH`

Lists every module with its `RETURN` status and key counts.

| Option | |
|---|---|
| `-m`, `--module NAME` | Only these modules: names, aliases (redirects) or globs; repeat or comma-separate |
| `--status STATUS` | Only modules with these `RETURN` statuses, e.g. `missing,placeholder` |
| `--format table\|json\|markdown` | Output format (default `table`) |
| `--config FILE` | Settings file to use instead of the project's own (see [Configuration](#configuration)) |

The count columns are the number of keys the docs (DOC), the code (CODE) and the
tests (TESTS) know about, and how many keys are undocumented (UNDOC) or stale
(STALE).

### `dr-ansible returns PATH MODULE`

Shows each return key of one module, with its status and evidence. MODULE is a
name, alias or fully qualified name, or a glob that matches exactly one module.

| Option | |
|---|---|
| `--include-common` | Also show the common return values (`changed`, `failed`, `msg`, ...) |
| `--run` | Also run the module's integration tests to record what it returns (see [Runtime evidence](#runtime-evidence---run)) |
| `--docker IMAGE` / `--local` | With `--run`: the ansible-test container to use, or run on this machine instead |
| `--format table\|json\|markdown` | Output format (default `table`) |
| `--config FILE` | Settings file |

### `dr-ansible draft PATH MODULE`

Prints a draft `RETURN` block.

| Option | |
|---|---|
| `--merge` / `--full` | Keep the existing `RETURN` byte for byte and add only the missing keys (default), or draft it all anew |
| `-o`, `--output FILE` | Write the draft to a file instead of stdout; never inside the module or action plugin directories |
| `--run`, `--docker IMAGE`, `--local` | As for `returns` |
| `--config FILE` | Settings file |

In merge mode, keys missing from inside an existing entry are listed as YAML
comments rather than edited in. For a module whose docs live in a sidecar `.yml`
file, the draft is the entries to paste under its `RETURN:` key.

## What the statuses mean

A module's `RETURN` is one of:

| Status | Meaning |
|---|---|
| `present` | Documented |
| `missing` | No `RETURN` at all |
| `placeholder` | `RETURN` exists but is empty or a placeholder such as `#` |
| `invalid` | `RETURN` is not valid YAML, or not a mapping of keys |
| `exempt` | On the allowlist of modules that return nothing worth documenting (e.g. `include_tasks`) |
| `unsupported` | A PowerShell module: not analysed |
| `error` | The module could not be analysed; the reason is shown and the run carries on |

Each key is one of:

| Status | Documented | In code | In tests |
|---|:-:|:-:|:-:|
| `ok` | ✓ | ✓ or ✗ | ✓ or ✗ (but at least one of code or tests) |
| `undocumented` | ✗ | ✓ | ✓ or ✗ |
| `test-only` | ✗ | ✗ | ✓ |
| `stale` | ✓ | ✗ | ✗ |

Keys the analysis cannot name, such as computed key names, are listed as
*unresolved* with their location rather than guessed.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Nothing needs attention (for `draft`: the draft was written) |
| `1` | Findings: a missing, placeholder or invalid `RETURN`, an analysis error, or undocumented, test-only or stale keys. Exempt and PowerShell modules never count. |
| `2` | Usage error, or a path, config file or module that cannot be used |

## Using `audit` as a CI gate

Only the listed modules count towards the exit code, so `--status` decides what
fails the build. To fail when any module has no `RETURN`, or only a placeholder:

```yaml
# .github/workflows/docs.yml in a collection repository
- uses: astral-sh/setup-uv@<pinned sha>
- run: uvx dr-ansible audit . --status missing,placeholder
```

Leave `--status` out to fail on every finding, including undocumented and stale
keys. `--format markdown` gives a table to paste into a pull request.

## Configuration

dr-ansible looks in the root of the project for `dr-ansible.toml`, then for a
`[tool.dr-ansible]` table in `pyproject.toml`; the first one found is used on its
own. `--config FILE` names a file explicitly.

```toml
# dr-ansible.toml
extend-exempt-modules = ["my_noop_module"]     # add to the default allowlist
common-return-keys = ["changed", "failed", "msg", "skipped", "invocation"]
extend-redact-patterns = ["passphrase"]
docker-image = "default"
```

| Setting | Default |
|---|---|
| `exempt-modules` | `gather_facts`, `import_playbook`, `import_role`, `import_tasks`, `include_role`, `include_tasks` |
| `common-return-keys` | `changed`, `deprecations`, `diff`, `exception`, `failed`, `invocation`, `msg`, `skipped`, `warnings` |
| `redact-patterns` | `key`, `password`, `secret`, `token` (case-insensitive regular expressions) |
| `docker-image` | `default` (the ansible-test container `--run` uses) |

Each list replaces the default; its `extend-` form adds to it instead.

## Runtime evidence: `--run`

Static evidence comes only from reading files. `--run` adds what the module
really returns: it runs the module's integration target with
`ansible-test integration`, in ansible-test's container by default (`--docker
IMAGE`) or on this machine (`--local`), with a recorder callback that notes each
result's keys, types and sample values.

- The project is copied to a temporary directory first; the original is never
  touched.
- Results of `no_log` tasks are skipped entirely. Keys matching the redaction
  patterns keep their type but never their value, and long values are
  truncated.
- It needs `ansible-test` (part of ansible-core) and, for the default mode,
  Docker.

Without `--run`, dr-ansible never imports or executes code from the project it
reads, and never writes to it.

## JSON output

`--format json` prints a report described by
[`schema/report.schema.json`](https://github.com/jeedo/dr-ansible/blob/main/schema/report.schema.json),
with a `schema_version` field. Modules are sorted by name and keys by path, so
the same input always gives the same output.

## Project documents

- [Requirements](https://github.com/jeedo/dr-ansible/blob/main/docs/requirements.md)
- [Architecture](https://github.com/jeedo/dr-ansible/blob/main/docs/architecture.md)
- [Implementation plan](https://github.com/jeedo/dr-ansible/blob/main/docs/plan.md)
- [Releasing](https://github.com/jeedo/dr-ansible/blob/main/docs/releasing.md) and
  [repository settings](https://github.com/jeedo/dr-ansible/blob/main/docs/repository-settings.md)

This project follows the spec-driven workflow from
[jeedo/spec-template](https://github.com/jeedo/spec-template).

## License

GPL-3.0-or-later; see
[LICENSE](https://github.com/jeedo/dr-ansible/blob/main/LICENSE). dr-ansible
imports ansible-core, which is GPLv3.
