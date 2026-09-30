# Fixture: keys static analysis cannot name. `result[f"{prefix}_id"]` computes
# its key at runtime, and finish() passes **extra from its caller to exit_json.
# Both must be reported as unresolved. `status` is an ordinary, resolvable key.
# RETURN is missing.

DOCUMENTATION = r"""
module: dynamic
short_description: Return keys whose names are computed
"""

from ansible.module_utils.basic import AnsibleModule


def finish(module, **extra):
    module.exit_json(changed=False, **extra)


def main():
    module = AnsibleModule(argument_spec=dict(prefix=dict(type="str", default="vm")))
    prefix = module.params["prefix"]

    result = {"status": "ok"}
    result[f"{prefix}_id"] = 1001
    finish(module, **result)


if __name__ == "__main__":
    main()
