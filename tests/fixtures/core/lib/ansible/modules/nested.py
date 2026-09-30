# Fixture: a module returning a nested dict, documented as `complex` with
# `contains`, like ansible.builtin.stat. The documentation and code agree.

DOCUMENTATION = r"""
module: nested
short_description: Return a nested dict
"""

RETURN = r"""
info:
    description: Facts about the path.
    returned: success
    type: complex
    contains:
        exists:
            description: Whether the path exists.
            returned: always
            type: bool
            sample: true
        size:
            description: Size in bytes.
            returned: success, path exists
            type: int
            sample: 1024
        owner:
            description: Owner of the path.
            returned: success, path exists
            type: str
            sample: root
"""

from ansible.module_utils.basic import AnsibleModule


def main():
    module = AnsibleModule(argument_spec=dict(path=dict(type="path", required=True)))
    info = {"exists": True}
    info["size"] = 1024
    info["owner"] = "root"
    module.exit_json(changed=False, info=info)


if __name__ == "__main__":
    main()
