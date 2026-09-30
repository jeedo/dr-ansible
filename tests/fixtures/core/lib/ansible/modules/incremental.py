# Fixture: a normal module that builds its result step by step and returns it
# with exit_json(**result). RETURN is missing.
# Patterns: dict(...) literal, result[key] = ..., result.update(...),
# result.setdefault(...), and a key set only under `if changed:`.

DOCUMENTATION = r"""
module: incremental
short_description: Build a result incrementally
options:
  path:
    description: Path to inspect.
    type: path
    required: true
"""

EXAMPLES = r"""
- incremental:
    path: /etc/hosts
"""

from ansible.module_utils.basic import AnsibleModule


def main():
    module = AnsibleModule(argument_spec=dict(path=dict(type="path", required=True)))
    path = module.params["path"]

    result = dict(changed=False, path=path)
    result["size"] = 42
    result.update(mode="0644", checksum=f"sha1:{path}")
    result.setdefault("owner", "root")

    changed = module.check_mode is False
    if changed:
        result["changed"] = True
        result["backup_file"] = path + ".bak"

    module.exit_json(**result)


if __name__ == "__main__":
    main()
