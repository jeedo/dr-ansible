# Fixture: RETURN is not valid YAML (the flow mapping is never closed).

DOCUMENTATION = r"""
module: invalid
short_description: Carry an unparsable RETURN block
"""

RETURN = r"""
value: {description: broken, type: str
"""

from ansible.module_utils.basic import AnsibleModule


def main():
    module = AnsibleModule(argument_spec=dict())
    module.exit_json(changed=False, value="x")


if __name__ == "__main__":
    main()
