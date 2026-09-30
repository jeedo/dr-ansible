# Fixture: a module whose result comes from a local helper that returns a dict.
# RETURN is present but out of step with the code: `owner` is returned and not
# documented, and `legacy_id` is documented and never returned.

DOCUMENTATION = r"""
module: helper
short_description: Return a dict built by a helper
"""

RETURN = r"""
name:
    description: The name that was looked up.
    returned: always
    type: str
    sample: web01
state:
    description: The state of the named thing.
    returned: success
    type: str
    sample: present
legacy_id:
    description: An identifier the module no longer returns.
    returned: success
    type: int
    sample: 7
"""

from ansible.module_utils.basic import AnsibleModule


def _build_result(name):
    return {"name": name, "state": "present", "owner": "root"}


def main():
    module = AnsibleModule(argument_spec=dict(name=dict(type="str", required=True)))
    result = _build_result(module.params["name"])
    module.exit_json(changed=False, **result)


if __name__ == "__main__":
    main()
