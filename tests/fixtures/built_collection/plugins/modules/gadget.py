# Fixture: a module in an installed (built) collection, identified by
# MANIFEST.json rather than galaxy.yml. RETURN is missing.

DOCUMENTATION = r"""
module: gadget
short_description: Manage a gadget
"""

from ansible.module_utils.basic import AnsibleModule


def main():
    module = AnsibleModule(argument_spec=dict())
    module.exit_json(changed=False, gadget="on")


if __name__ == "__main__":
    main()
