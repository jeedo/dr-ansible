# Fixture: the module half of a hybrid. The action plugin
# lib/ansible/plugins/action/hybrid.py runs this module with _execute_module and
# merges its result with keys of its own, like ansible.builtin.copy.

DOCUMENTATION = r"""
module: hybrid
short_description: A module wrapped by an action plugin
"""

RETURN = r"""
dest:
    description: Destination path.
    returned: success
    type: str
    sample: /tmp/dest
checksum:
    description: Checksum of the destination file.
    returned: success
    type: str
    sample: 6e642bb8dd5c2e027bf21dd923337cbb4214f827
"""

from ansible.module_utils.basic import AnsibleModule


def main():
    module = AnsibleModule(argument_spec=dict(dest=dict(type="path", required=True)))
    dest = module.params["dest"]
    module.exit_json(changed=True, dest=dest, checksum="6e642bb8dd5c2e02")


if __name__ == "__main__":
    main()
